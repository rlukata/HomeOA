"""Jobs that charge or escalate against owners wait for go-live, judged by each run's own
date, and dates before go-live are skipped for good."""

import datetime as dt

import pytest

from homeoa.access.testing import make_live
from homeoa.core import status
from homeoa.core.models import Association
from homeoa.jobs import periodic, run_due
from homeoa.jobs.models import ScheduledRun
from homeoa.jobs.scheduler import unregister

CALLS: list = []
BILLING = "t.billing"
OTHER = "t.other"


@pytest.fixture
def jobs():
    from homeoa.jobs import scheduler

    saved = dict(scheduler._registry)
    scheduler._registry.clear()
    CALLS.clear()

    def record(name):
        def run(association, as_of):
            CALLS.append((name, as_of))

        return run

    periodic(BILLING, "0 6 * * *", waits_for_go_live=True)(record(BILLING))
    periodic(OTHER, "0 6 * * *")(record(OTHER))
    yield
    unregister(BILLING)
    unregister(OTHER)
    scheduler._registry.update(saved)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR", timezone="UTC")
    Association.objects.filter(pk=a.pk).update(created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    a.refresh_from_db()
    return a


NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 10, 15)


def _dates(name):
    return [d for n, d in CALLS if n == name]


def _fresh(assoc):
    return Association.objects.get(pk=assoc.pk)


def test_billing_job_does_not_run_without_a_go_live_date(jobs, assoc):
    run_due(now=NOW, association=assoc)
    run_due(as_of=TODAY, association=assoc)
    assert _dates(BILLING) == []
    assert not ScheduledRun.all_associations.filter(job_name=BILLING).exists()


def test_billing_job_does_not_run_while_go_live_is_in_the_future(jobs, assoc):
    make_live(assoc, since=TODAY + dt.timedelta(days=1))
    run_due(now=NOW, association=_fresh(assoc))
    run_due(as_of=TODAY, association=_fresh(assoc))
    assert _dates(BILLING) == []


def test_a_job_that_does_not_wait_runs_while_setting_up(jobs, assoc):
    run_due(now=NOW, association=assoc)
    assert TODAY in _dates(OTHER)


def test_catch_up_across_go_live_runs_only_from_go_live_and_never_replays(jobs, assoc):
    go_live = TODAY - dt.timedelta(days=3)
    make_live(assoc, since=go_live)
    run_due(now=NOW, association=_fresh(assoc))
    assert _dates(BILLING) == [go_live + dt.timedelta(days=i) for i in range(4)]
    assert len(_dates(OTHER)) == 8  # the full 7-day catch-up window plus today
    CALLS.clear()
    run_due(now=NOW + dt.timedelta(days=1), association=_fresh(assoc))
    assert _dates(BILLING) == [TODAY + dt.timedelta(days=1)]


def test_an_explicit_run_before_go_live_is_skipped_even_with_rerun(jobs, assoc):
    make_live(assoc, since=TODAY)
    run_due(as_of=TODAY - dt.timedelta(days=1), association=_fresh(assoc), rerun=True)
    assert _dates(BILLING) == []
    run_due(as_of=TODAY, association=_fresh(assoc))
    assert _dates(BILLING) == [TODAY]


def test_the_run_date_decides_not_the_clock(jobs, assoc):
    make_live(assoc, since=TODAY)
    # The clock is well past go-live, but the run is for a date before it.
    run_due(as_of=TODAY - dt.timedelta(days=5), now=NOW, association=_fresh(assoc))
    assert _dates(BILLING) == []
    # The clock is before go-live, but the run is for go-live day.
    run_due(as_of=TODAY, now=NOW - dt.timedelta(days=30), association=_fresh(assoc))
    assert _dates(BILLING) == [TODAY]


def test_paused_resident_access_does_not_stop_billing(jobs, assoc):
    make_live(assoc, since=TODAY - dt.timedelta(days=30))
    status.pause_residents(assoc, reason="Emergency")
    run_due(as_of=TODAY, association=_fresh(assoc))
    assert _dates(BILLING) == [TODAY]


def test_failed_runs_before_go_live_are_not_retried(jobs, assoc):
    before = TODAY - dt.timedelta(days=2)
    ScheduledRun.all_associations.create(
        association=assoc, job_name=BILLING, as_of=before, status=ScheduledRun.Status.FAILED
    )
    make_live(assoc, since=TODAY - dt.timedelta(days=1))
    run_due(now=NOW, association=_fresh(assoc))
    assert before not in _dates(BILLING)


def test_the_jobs_that_charge_owners_wait_for_go_live():
    from homeoa.jobs import periodic_jobs

    waiting = {name for name, job in periodic_jobs().items() if job.waits_for_go_live}
    assert {
        "billing.post_assessments",
        "billing.assess_late_fees",
        "billing.post_special_installments",
        "statements.delinquency_ladder",
        "payments.autopay",
    } <= waiting
    assert not periodic_jobs()["ops.backup_check"].waits_for_go_live


def test_job_history_shows_which_jobs_wait_while_setting_up(client, assoc):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("admin@example.org")
    grant(user, "sys_admin", association=assoc)
    client.force_login(user)
    assert b"Waits for go-live" in client.get("/settings/jobs/").content
    make_live(assoc)
    assert b"Waits for go-live" not in client.get("/settings/jobs/").content
