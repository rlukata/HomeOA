"""Backup staleness job (ops.backup_check) and the recipients deploy check."""

import datetime as dt
import json

import pytest
from django.core import mail
from django.test import override_settings

from homeoa import events
from homeoa.core.checks import check_backup_recipients
from homeoa.core.models import Association
from homeoa.core.ops import backup_status, check_backups
from homeoa.jobs import periodic_jobs

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)


def write(path, name, when):
    (path / f"last_{name}.json").write_text(json.dumps({"time": when.isoformat()}))


@pytest.fixture
def status_dir(tmp_path, settings):
    settings.HOMEOA_BACKUP_STATUS_DIR = str(tmp_path)
    settings.HOMEOA_BACKUP_MAX_AGE_HOURS = 36
    settings.ADMINS = ["ops@example.org"]
    return tmp_path


@pytest.fixture
def received():
    got = []
    events.subscribe("ops.backup_stale", got.append)
    yield got
    events.unsubscribe("ops.backup_stale", got.append)


def test_not_configured_is_silent(settings):
    settings.HOMEOA_BACKUP_STATUS_DIR = ""
    assert backup_status(NOW) is None


def test_fresh_success_is_ok(status_dir):
    write(status_dir, "success", NOW - dt.timedelta(hours=20))
    write(status_dir, "failure", NOW - dt.timedelta(days=3))
    assert not backup_status(NOW).stale


@pytest.mark.parametrize(
    ("success_age", "failure_age", "reason"),
    [(None, None, "never"), (37, None, "old"), (20, 2, "failed"), (None, 1, "failed")],
)
def test_stale_reasons(status_dir, success_age, failure_age, reason):
    if success_age is not None:
        write(status_dir, "success", NOW - dt.timedelta(hours=success_age))
    if failure_age is not None:
        write(status_dir, "failure", NOW - dt.timedelta(hours=failure_age))
    status = backup_status(NOW)
    assert status.stale and status.reason == reason


def test_garbage_file_counts_as_missing(status_dir):
    (status_dir / "last_success.json").write_text("{not json")
    assert backup_status(NOW).reason == "never"


@pytest.mark.django_db
def test_check_emails_admins_and_publishes(status_dir, received):
    assoc = Association.objects.create(name="Cedar Row", short_name="CR")
    write(status_dir, "success", NOW - dt.timedelta(hours=50))
    check_backups(assoc, now=NOW)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ops@example.org"]
    assert "36 hours" in mail.outbox[0].body
    assert len(received) == 1
    assert received[0].reason == "old"
    assert received[0].association_id == assoc.pk


@pytest.mark.django_db
def test_check_quiet_when_healthy(status_dir, received):
    assoc = Association.objects.create(name="Cedar Row", short_name="CR")
    write(status_dir, "success", NOW - dt.timedelta(hours=1))
    check_backups(assoc, now=NOW)
    assert mail.outbox == [] and received == []


def test_job_is_registered_daily():
    import homeoa.core.tasks  # noqa: F401

    job = periodic_jobs()["ops.backup_check"]
    assert job.cron == "0 8 * * *"


@pytest.mark.parametrize(("keys", "warns"), [([], True), (["age1a"], True), (["a", "b"], False)])
def test_recipients_check(keys, warns):
    with override_settings(HOMEOA_BACKUP_MIN_RECIPIENTS=2, HOMEOA_BACKUP_AGE_RECIPIENTS=keys):
        result = check_backup_recipients()
    assert bool(result) is warns
    if warns:
        assert result[0].id == "homeoa.W002"


def test_recipients_check_off_outside_prod():
    with override_settings(HOMEOA_BACKUP_MIN_RECIPIENTS=0, HOMEOA_BACKUP_AGE_RECIPIENTS=[]):
        assert check_backup_recipients() == []
