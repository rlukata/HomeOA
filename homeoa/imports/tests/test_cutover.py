import datetime as dt
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.governance.models import BoardTerm
from homeoa.imports import engine, services
from homeoa.imports.cutover import readiness, set_records_requested
from homeoa.imports.diff import services as diff
from homeoa.imports.models import ImportRun
from homeoa.ledger.services import set_locked_through
from homeoa.registry.models import Person

FIX = Path(__file__).parents[1] / "fixtures"
BASE = FIX / "econdo_sample"
LATER = FIX / "econdo_sample_later_unit7_latefee"
AS_OF = dt.date(2026, 10, 15)
TODAY = dt.date(2026, 11, 20)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def load(assoc, folder, mode):
    from homeoa.ledger.chart import load_chart_of_accounts

    load_chart_of_accounts(assoc)
    uploads = [(p.name, p.read_bytes()) for p in sorted(folder.glob("*.csv"))]
    r = services.create_run(assoc, source="econdo", mode=mode, as_of=AS_OF, uploads=uploads)
    assert not engine.commit(r)["problems"]
    r.refresh_from_db()
    return r


def board(assoc, name, *, email="x@example.org", login=True):
    user = get_user_model().objects.create_user(f"{name}@example.org") if login else None
    p = Person.objects.create(association=assoc, full_name=name, email=email, user=user)
    BoardTerm.objects.create(
        association=assoc, person=p, office="director", start=dt.date(2026, 1, 1)
    )
    return p


def by_key(r):
    return {c.key: c for c in r.checks}


def test_empty_association_is_not_ready(assoc):
    r = readiness(assoc, TODAY)
    assert not r.ready
    assert not any(c.ok for c in r.checks)
    assert r.latest_diff is None


def test_all_checks_pass(assoc):
    load(assoc, BASE, ImportRun.Mode.COMMIT)
    set_locked_through(assoc, AS_OF)
    diff.run_diff(load(assoc, BASE, ImportRun.Mode.COMPARISON))
    board(assoc, "Pat")
    set_records_requested(assoc, True)
    from homeoa.imports.cutover import set_clean_months_required

    set_clean_months_required(assoc, 1)
    r = readiness(assoc, TODAY)
    assert r.ready, [(c.key, c.detail) for c in r.checks]
    assert r.clean_months == 1


def test_dirty_diff_and_missing_login_block(assoc):
    load(assoc, BASE, ImportRun.Mode.COMMIT)
    set_locked_through(assoc, dt.date(2026, 9, 30))  # before the import date
    diff.run_diff(load(assoc, LATER, ImportRun.Mode.COMPARISON))
    board(assoc, "Pat")
    board(assoc, "Sam", email="", login=False)
    checks = by_key(readiness(assoc, TODAY))
    assert checks["import"].ok
    assert not checks["lock"].ok
    assert not checks["parallel"].ok
    assert not checks["people"].ok
    assert checks["people"].items == ["Sam: no login, no email"]


def test_clean_months_setting_gates_ready(assoc):
    from homeoa.audit.models import AuditEvent
    from homeoa.imports.cutover import clean_months_required, set_clean_months_required

    assert clean_months_required(assoc) == 2
    load(assoc, BASE, ImportRun.Mode.COMMIT)
    set_locked_through(assoc, AS_OF)
    diff.run_diff(load(assoc, BASE, ImportRun.Mode.COMPARISON))
    board(assoc, "Pat")
    set_records_requested(assoc, True)
    r = readiness(assoc, TODAY)
    assert not by_key(r)["months"].ok and "1 of 2" in by_key(r)["months"].detail
    assert not r.ready
    set_clean_months_required(assoc, 1)
    assert AuditEvent.objects.filter(message__icontains="clean months").exists()
    r = readiness(assoc, TODAY)
    assert by_key(r)["months"].ok and r.ready


def test_month_statuses(assoc):
    load(assoc, BASE, ImportRun.Mode.COMMIT)
    diff.run_diff(load(assoc, BASE, ImportRun.Mode.COMPARISON))  # October, clean
    r = readiness(assoc, dt.date(2026, 12, 5))
    assert [(m.month, m.status) for m in r.months] == [
        (dt.date(2026, 12, 1), "missing"),
        (dt.date(2026, 11, 1), "missing"),
        (dt.date(2026, 10, 1), "clean"),
    ]


def test_month_with_differences(assoc):
    load(assoc, BASE, ImportRun.Mode.COMMIT)
    diff.run_diff(load(assoc, LATER, ImportRun.Mode.COMPARISON))
    assert readiness(assoc, TODAY).months[-1].status == "differences"


def test_counts(assoc):
    from homeoa.access.models import Invitation
    from homeoa.registry.models import OwnershipPeriod, OwnershipShare, Unit

    unit = Unit.objects.create(association=assoc, number="A")
    period = OwnershipPeriod.objects.create(association=assoc, unit=unit, start=dt.date(2020, 1, 1))
    owners = []
    for name, login in (("Ann", True), ("Bob", False)):
        user = get_user_model().objects.create_user(f"{name}@example.org") if login else None
        p = Person.objects.create(association=assoc, full_name=name, user=user)
        OwnershipShare.objects.create(association=assoc, period=period, person=p)
        owners.append(p)
    Invitation.objects.create(
        association=assoc, person=owners[0], email="a@example.org", status=Invitation.Status.SENT
    )
    r = readiness(assoc, TODAY)
    assert (r.owners.total, r.owners.invited, r.owners.accepted, r.owners.consent) == (2, 1, 1, 0)
    assert isinstance(r.open_todos, int)
    assert r.unverified_rules >= 0


def test_unverified_rule_count_counts_statute_claims(assoc, monkeypatch):
    from types import SimpleNamespace

    from homeoa import rules
    from homeoa.imports.cutover import unverified_rule_count
    from homeoa.rules.base import Basis

    vals = [
        SimpleNamespace(basis=Basis.STATUTE, verified=False),
        SimpleNamespace(basis=Basis.STATUTE, verified=True),
        SimpleNamespace(basis=Basis.POLICY, verified=False),
    ]
    monkeypatch.setattr(rules, "all_values", lambda a, on=None: vals)
    assert unverified_rule_count(assoc, TODAY) == 1


def test_records_checkbox_needs_finance_manage_and_is_audited(client, assoc):
    from homeoa.audit.models import AuditEvent

    url = reverse("imports:cutover")
    reader = get_user_model().objects.create_user("r@example.org")
    grant(reader, association=assoc)
    client.force_login(reader)
    assert client.get(url).status_code == 200  # board reads; finance.manage needed to write
    assert client.post(url, {"action": "records", "requested": "1"}).status_code == 403
    assert not AuditEvent.objects.filter(message__icontains="records delivery").exists()
    user = get_user_model().objects.create_user("t@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    assert client.post(url, {"action": "records", "requested": "1"}).status_code == 302
    assoc.refresh_from_db()
    assert by_key(readiness(assoc, TODAY))["records"].ok
    assert AuditEvent.objects.filter(message__icontains="records delivery").exists()
    client.post(url, {"action": "months", "clean_months_required": "3"})
    assoc.refresh_from_db()
    assert readiness(assoc, TODAY).required_months == 3


def test_page_needs_finance_read_and_renders(client, assoc):
    url = reverse("imports:cutover")
    outsider = get_user_model().objects.create_user("o@example.org")
    client.force_login(outsider)
    assert client.get(url).status_code in (302, 403)
    user = get_user_model().objects.create_user("t@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    resp = client.get(url)
    assert resp.status_code == 200
    assert "Cutover readiness" in resp.text
    assert "Not ready" in resp.text
