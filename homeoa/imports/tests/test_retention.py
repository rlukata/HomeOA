import datetime as dt
import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.imports import retention, services
from homeoa.imports.models import ImportFile, ImportRun
from homeoa.jobs import run_due

AS_OF = dt.date(2026, 10, 7)
CSV = b"Unit,Balance\n1,10.00\n"


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR", timezone="UTC")
    with association_context(a):
        yield a


def make_run(assoc, *, days_ago=100, status=ImportRun.Status.COMMITTED):
    run = services.create_run(
        assoc,
        source="csv",
        mode=ImportRun.Mode.COMMIT,
        as_of=AS_OF,
        uploads=[("units.csv", CSV)],
    )
    run.status = status
    run.finished_at = timezone.make_aware(
        dt.datetime.combine(AS_OF - dt.timedelta(days=days_ago), dt.time(12))
    )
    run.save()
    return run


def test_upload_stores_hash(assoc):
    run = make_run(assoc)
    f = run.files.get()
    assert f.sha256 == hashlib.sha256(CSV).hexdigest()


def test_purge_keeps_record_hash_and_audits(assoc):
    run = make_run(assoc)
    report = {"totals": {"units": 1}}
    run.report = report
    run.save()
    assert retention.purge_run(run) is True
    run.refresh_from_db()
    f = ImportFile.objects.get(run=run)
    assert run.purged_at is not None
    assert f.content == ""
    assert f.sha256 == hashlib.sha256(CSV).hexdigest()
    assert run.report == report and run.config
    ev = AuditEvent.objects.get(action="imports.files_purged")
    assert ev.object_id == str(run.pk)
    # Idempotent: a second purge does nothing and writes no second event.
    assert retention.purge_run(run) is False
    assert AuditEvent.objects.filter(action="imports.files_purged").count() == 1


def test_purge_expired_skips_recent_and_uncommitted(assoc):
    old = make_run(assoc, days_ago=90)
    recent = make_run(assoc, days_ago=89)
    stored = make_run(assoc, days_ago=200, status=ImportRun.Status.STORED)
    previewed = make_run(assoc, days_ago=200, status=ImportRun.Status.PREVIEWED)
    assert retention.purge_expired(assoc, AS_OF) == 1
    assert retention.purge_expired(assoc, AS_OF) == 0
    for r in (old, recent, stored, previewed):
        r.refresh_from_db()
    assert old.purged_at is not None
    assert recent.purged_at is None and stored.purged_at is None and previewed.purged_at is None
    assert recent.files.get().content


def test_retention_setting(assoc):
    assoc.settings = {"import_retention_days": 10}
    assoc.save()
    run = make_run(assoc, days_ago=10)
    assert retention.purge_expired(assoc, AS_OF) == 1
    run.refresh_from_db()
    assert run.purged_at


def test_job_uses_as_of(assoc):
    run = make_run(assoc, days_ago=0)
    run_due(as_of=AS_OF, association=assoc, names=["imports.purge_files"])
    run.refresh_from_db()
    assert run.purged_at is None
    run_due(as_of=AS_OF + dt.timedelta(days=90), association=assoc, names=["imports.purge_files"])
    run.refresh_from_db()
    assert run.purged_at is not None


def test_purge_now_button(client, assoc):
    run = make_run(assoc, days_ago=1)
    user = get_user_model().objects.create_user("treasurer@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    page = client.get(f"/units/import/{run.pk}/").content.decode()
    assert 'data-testid="import-purge"' in page
    resp = client.post(f"/units/import/{run.pk}/purge/")
    assert resp.status_code == 302
    run.refresh_from_db()
    assert run.purged_at is not None
    page = client.get(f"/units/import/{run.pk}/").content.decode()
    assert 'data-testid="import-purged"' in page
    assert 'data-testid="import-purge"' not in page


def test_purge_needs_finance_manage(client, assoc):
    run = make_run(assoc, days_ago=1)
    user = get_user_model().objects.create_user("someone@example.org")
    client.force_login(user)
    resp = client.post(f"/units/import/{run.pk}/purge/")
    assert resp.status_code in (302, 403)
    run.refresh_from_db()
    assert run.purged_at is None
