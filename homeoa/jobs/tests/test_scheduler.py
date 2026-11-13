import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from django.core.management import CommandError, call_command
from django.urls import reverse

from homeoa.core.context import get_current_association
from homeoa.core.models import Association
from homeoa.jobs import periodic, periodic_jobs, run_due
from homeoa.jobs.models import ScheduledRun
from homeoa.jobs.scheduler import PeriodicJob, unregister
from homeoa.jobs.testing import capture_jobs
from tests.scoped.models import Widget

CALLS: list = []


@pytest.fixture
def jobs():
    names = []

    def add(name, cron, func=None):
        def default(association, as_of):
            CALLS.append((name, association.pk, as_of, get_current_association()))

        periodic(name, cron)(func or default)
        names.append(name)

    # Isolate from jobs that real apps register (e.g. ops.backup_check, billing.post_assessments).
    from homeoa.jobs import scheduler

    saved = dict(scheduler._registry)
    scheduler._registry.clear()
    CALLS.clear()
    yield add
    for name in names:
        unregister(name)
    scheduler._registry.update(saved)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    Association.objects.filter(pk=a.pk).update(created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    a.refresh_from_db()
    return a


def utc(*args):
    return dt.datetime(*args, tzinfo=dt.UTC)


def test_registry_and_invalid_cron(jobs):
    jobs("t.daily", "0 6 * * *")
    assert periodic_jobs()["t.daily"].cron == "0 6 * * *"
    with pytest.raises(ValueError):
        periodic("t.bad", "not cron")
    with pytest.raises(ValueError):
        periodic("t.daily", "0 6 * * *")(lambda a, d: None)


def test_fire_times_are_local():
    job = PeriodicJob("x", "0 6 11 * *", lambda a, d: None)
    tz = ZoneInfo("America/Los_Angeles")
    times = job.fire_times(dt.date(2026, 9, 11), tz)
    assert [t.hour for t in times] == [6]
    assert times[0].utcoffset() == dt.timedelta(hours=-7)
    assert not job.is_due(dt.date(2026, 9, 12), tz)


def test_as_of_runs_due_jobs_in_association_context(jobs, assoc):
    jobs("t.monthly", "0 6 11 * *")
    jobs("t.other_day", "0 6 12 * *")
    runs = run_due(as_of=dt.date(2026, 9, 11))
    assert [r.job_name for r in runs] == ["t.monthly"]
    assert [("t.monthly", assoc.pk, dt.date(2026, 9, 11), assoc)] == CALLS
    assert runs[0].status == ScheduledRun.Status.SUCCEEDED
    # Idempotent per association/job/date, unless rerun.
    assert run_due(as_of=dt.date(2026, 9, 11)) == []
    assert len(run_due(as_of=dt.date(2026, 9, 11), rerun=True)) == 1
    assert ScheduledRun.all_associations.get().attempts == 2


def test_tick_respects_local_time(jobs, assoc):
    jobs("t.six", "0 6 * * *")
    # 05:30 Pacific (12:30 UTC): today is not yet due; only the catch-up window runs.
    early = run_due(now=dt.datetime(2026, 9, 11, 12, 30, tzinfo=dt.UTC))
    assert dt.date(2026, 9, 11) not in [r.as_of for r in early]
    # 06:05 Pacific: due, and as_of is the local date.
    runs = run_due(now=dt.datetime(2026, 9, 11, 13, 5, tzinfo=dt.UTC))
    assert [r.as_of for r in runs] == [dt.date(2026, 9, 11)]
    # 02:00 UTC on the 12th is still the 11th in Pacific: already done.
    assert run_due(now=dt.datetime(2026, 9, 12, 2, 0, tzinfo=dt.UTC)) == []


def test_failure_rolls_back_and_is_recorded(jobs, assoc):
    def broken(association, as_of):
        Widget.objects.create(name="partial")
        raise RuntimeError("job broke")

    jobs("t.broken", "0 0 * * *", broken)
    [run] = run_due(as_of=dt.date(2026, 9, 11))
    assert run.status == ScheduledRun.Status.FAILED
    assert "job broke" in run.error
    assert not Widget.all_associations.exists()
    # Failed runs are retried.
    assert len(run_due(as_of=dt.date(2026, 9, 11))) == 1


def test_per_association(jobs, assoc):
    other = Association.objects.create(name="Other", short_name="O")
    jobs("t.daily", "0 6 * * *")
    run_due(as_of=dt.date(2026, 9, 11))
    assert sorted(c[1] for c in CALLS) == sorted([assoc.pk, other.pk])
    CALLS.clear()
    run_due(as_of=dt.date(2026, 9, 12), association=other)
    assert [c[1] for c in CALLS] == [other.pk]


def test_run_scheduled_command(jobs, assoc, capsys):
    jobs("t.monthly", "0 6 11 * *")
    call_command("run_scheduled", "--as-of", "2026-09-11")
    out = capsys.readouterr().out
    assert "t.monthly 2026-09-11 succeeded" in out
    assert CALLS[0][2] == dt.date(2026, 9, 11)
    with pytest.raises(CommandError):
        call_command("run_scheduled", "--as-of", "11/09/2026")
    with pytest.raises(CommandError):
        call_command("run_scheduled", "--as-of", "2026-09-11", "--job", "nope")


def test_run_scheduled_command_reports_failure(jobs, assoc):
    def broken(association, as_of):
        raise RuntimeError

    jobs("t.broken", "0 0 * * *", broken)
    with pytest.raises(CommandError):
        call_command("run_scheduled", "--as-of", "2026-09-11")


def test_tick_task_runs_via_queue(jobs, assoc):
    from homeoa.jobs.tasks import tick

    jobs("t.always", "* * * * *")
    with capture_jobs() as queue:
        tick.defer(timestamp=0)
        assert queue.run_all() == 1
    assert len(CALLS) == 8  # today plus the 7-day catch-up window


def test_tick_does_nothing_while_the_date_is_pinned(jobs, assoc, settings):
    from homeoa.jobs.tasks import tick

    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    jobs("t.always", "* * * * *")
    with capture_jobs() as queue:
        tick.defer(timestamp=0)
        assert queue.run_all() == 1
    assert CALLS == []


def test_tick_is_registered_periodic():
    from procrastinate.contrib.django import app

    assert "homeoa.jobs.tick" in app.tasks
    ids = [p.periodic_id for p in app.periodic_registry.periodic_tasks.values()]
    assert "homeoa.jobs.tick" in ids


def test_history_view(client, settings, jobs, assoc):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    jobs("t.monthly", "0 6 11 * *")
    run_due(as_of=dt.date(2026, 9, 11))
    resp = client.get(reverse("jobs:index"))  # middleware sets the association
    assert resp.status_code == 200
    assert b"t.monthly" in resp.content
    assert b"Background jobs" in resp.content


def test_history_view_gated(client, settings, db):
    assert client.get(reverse("jobs:index")).status_code == 302  # to /login/


def test_catch_up_runs_missed_dates_in_order(jobs, assoc):
    jobs("t.six", "0 6 * * *")
    run_due(now=utc(2026, 9, 10, 14))  # ran on the 10th
    CALLS.clear()
    # Worker down; next tick on the 13th after 06:00 local: 11th, 12th, 13th.
    runs = run_due(now=utc(2026, 9, 13, 14))
    assert [r.as_of for r in runs] == [dt.date(2026, 9, d) for d in (11, 12, 13)]
    assert [c[2] for c in CALLS] == [dt.date(2026, 9, d) for d in (11, 12, 13)]
    assert run_due(now=utc(2026, 9, 13, 15)) == []


def test_catch_up_capped_by_lookback(jobs, assoc, settings):
    settings.HOMEOA_SCHEDULER_CATCHUP_DAYS = 3
    jobs("t.six", "0 6 * * *")
    run_due(now=utc(2026, 8, 1, 14))
    runs = run_due(now=utc(2026, 9, 13, 14))
    assert [r.as_of for r in runs] == [dt.date(2026, 9, d) for d in (10, 11, 12, 13)]


def test_catch_up_never_before_association_created(jobs, assoc):
    Association.objects.filter(pk=assoc.pk).update(created_at=utc(2026, 9, 12, 20))
    assoc.refresh_from_db()
    jobs("t.six", "0 6 * * *")
    runs = run_due(now=utc(2026, 9, 13, 14))
    assert [r.as_of for r in runs] == [dt.date(2026, 9, 12), dt.date(2026, 9, 13)]


def test_no_catch_up_runs_latest_only(jobs, assoc):
    periodic("t.latest", "0 6 * * *", catch_up=False)(
        lambda a, d: CALLS.append(("t.latest", a.pk, d, None))
    )
    try:
        CALLS.clear()
        run_due(now=utc(2026, 9, 10, 14))
        runs = run_due(now=utc(2026, 9, 13, 14))
        assert [r.as_of for r in runs] == [dt.date(2026, 9, 13)]
    finally:
        unregister("t.latest")


def test_running_row_is_skipped_unless_stale(jobs, assoc):
    from django.utils import timezone

    jobs("t.daily", "0 6 * * *")
    day = dt.date(2026, 9, 11)
    run = ScheduledRun.all_associations.create(
        association=assoc,
        job_name="t.daily",
        as_of=day,
        status=ScheduledRun.Status.RUNNING,
        started_at=timezone.now(),
        attempts=1,
    )
    assert run_due(as_of=day) == []
    assert CALLS == []
    ScheduledRun.all_associations.filter(pk=run.pk).update(
        started_at=timezone.now() - dt.timedelta(hours=2)
    )
    [again] = run_due(as_of=day)
    assert again.status == ScheduledRun.Status.SUCCEEDED and again.attempts == 2


def test_failed_retries_capped_and_event_published(jobs, assoc, settings):
    from homeoa.events import subscribe, unsubscribe

    settings.HOMEOA_SCHEDULER_MAX_ATTEMPTS = 2
    seen = []

    def handler(event):
        seen.append(event)

    subscribe("jobs.run_failed", handler)

    def broken(association, as_of):
        raise RuntimeError

    jobs("t.broken", "0 0 * * *", broken)
    day = dt.date(2026, 9, 11)
    try:
        assert len(run_due(as_of=day)) == 1
        assert seen == []
        assert len(run_due(as_of=day)) == 1
        assert run_due(as_of=day) == []  # capped
    finally:
        unsubscribe("jobs.run_failed", handler)
    [event] = seen
    assert (event.job_name, event.as_of, event.attempts) == ("t.broken", day, 2)
    assert event.association_id == assoc.pk


def test_failed_date_retried_after_later_success(jobs, assoc, settings):
    from homeoa.events import subscribe, unsubscribe

    settings.HOMEOA_SCHEDULER_MAX_ATTEMPTS = 3
    seen = []

    def handler(event):
        seen.append(event)

    def flaky(association, as_of):
        CALLS.append(("t.flaky", association.pk, as_of, None))
        if as_of == dt.date(2026, 10, 1):
            raise RuntimeError

    subscribe("jobs.run_failed", handler)
    jobs("t.flaky", "0 6 * * *", flaky)
    try:
        run_due(now=utc(2026, 9, 30, 14))
        CALLS.clear()
        run_due(now=utc(2026, 10, 1, 14))  # Oct 1 fails (attempt 1)
        run_due(now=utc(2026, 10, 2, 14))  # Oct 1 retried (2), Oct 2 succeeds
        assert [c[2] for c in CALLS] == [
            dt.date(2026, 10, 1),
            dt.date(2026, 10, 1),
            dt.date(2026, 10, 2),
        ]
        assert seen == []
        runs = run_due(now=utc(2026, 10, 2, 15))  # Oct 1 attempt 3: cap
        assert [(r.as_of, r.attempts) for r in runs] == [(dt.date(2026, 10, 1), 3)]
        assert [e.as_of for e in seen] == [dt.date(2026, 10, 1)]
        assert run_due(now=utc(2026, 10, 2, 16)) == []
    finally:
        unsubscribe("jobs.run_failed", handler)
