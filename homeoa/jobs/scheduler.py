"""Periodic job registry and the due-job runner. See the ``homeoa.jobs`` docstring."""

from __future__ import annotations

import datetime as dt
import logging
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from croniter import croniter
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from homeoa.audit import audit_context
from homeoa.core.context import association_context
from homeoa.events import publish
from homeoa.plugins import slots

if TYPE_CHECKING:
    from homeoa.core.models import Association

logger = logging.getLogger(__name__)

JobFunc = Callable[["Association", dt.date], object]


@dataclass(frozen=True)
class PeriodicJob:
    name: str
    cron: str
    func: JobFunc
    description: str = ""
    catch_up: bool = True
    module: str | None = None
    waits_for_go_live: bool = False

    def fire_times(self, day: dt.date, tz: ZoneInfo) -> list[dt.datetime]:
        """Local fire times of ``cron`` on ``day`` in ``tz``."""
        start = dt.datetime.combine(day, dt.time.min, tzinfo=tz) - dt.timedelta(seconds=1)
        it = croniter(self.cron, start)
        times = []
        while True:
            nxt = it.get_next(dt.datetime)
            if nxt.date() != day:
                return times
            times.append(nxt)

    def is_due(self, day: dt.date, tz: ZoneInfo) -> bool:
        return bool(self.fire_times(day, tz)[:1])


_registry: dict[str, PeriodicJob] = {}


def periodic(
    name: str,
    cron: str,
    description: str = "",
    *,
    catch_up: bool = True,
    module: str | None = None,
    waits_for_go_live: bool = False,
) -> Callable[[JobFunc], JobFunc]:
    """Register ``func(association, as_of)`` to run on each local date ``cron`` fires.

    With ``catch_up`` (default) the worker runs every missed date in order; without it, only
    the latest due date runs. ``module`` names the optional module that owns the job
    (``None`` for core); the job is recorded under it in the daily jobs slot. With
    ``waits_for_go_live`` (jobs that charge or escalate against owners) the job does not run
    for any date before the association's go-live date, nor while it has none; those dates
    are skipped for good, not made up later.
    """
    if not croniter.is_valid(cron):
        raise ValueError(f"Invalid cron expression {cron!r} for {name!r}.")

    def decorator(func: JobFunc) -> JobFunc:
        existing = _registry.get(name)
        if existing is not None and existing.func.__qualname__ != func.__qualname__:
            raise ValueError(f"Periodic job {name!r} is already registered.")
        _registry[name] = PeriodicJob(
            name,
            cron,
            func,
            description or (func.__doc__ or ""),
            catch_up,
            module,
            waits_for_go_live,
        )
        if existing is None:
            slots.register(slots.DAILY_JOBS, name, module=module)
        return func

    return decorator


def unregister(name: str) -> None:
    _registry.pop(name, None)


def periodic_jobs() -> dict[str, PeriodicJob]:
    return dict(sorted(_registry.items()))


def job_module(name: str, association: Association | None = None) -> str | None:
    """The module that owns job ``name``, read from the daily jobs slot (``None``: core)."""
    for c in slots.contributions(slots.DAILY_JOBS, association):
        if c.value == name:
            return c.module
    return None


def daily_jobs(association: Association | None = None) -> list[PeriodicJob]:
    """The registered jobs listed in the daily jobs slot for ``association``, by name."""
    listed = set(slots.values(slots.DAILY_JOBS, association))
    return [job for name, job in periodic_jobs().items() if name in listed]


def runs_on(job: PeriodicJob, assoc: Association, day: dt.date) -> bool:
    """Whether ``job`` may run for run date ``day``: a job that waits for go-live runs only
    on or after the association's go-live date. Compares the run date, never the clock."""
    if not job.waits_for_go_live:
        return True
    from homeoa.core import status

    go_live = status.go_live_date(assoc)
    return go_live is not None and day >= go_live


def _associations(association: Association | None) -> Iterable[Association]:
    from homeoa.core.models import Association

    if association is not None:
        return [association]
    return Association.objects.order_by("pk")


def _setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def catchup_days() -> int:
    return _setting("HOMEOA_SCHEDULER_CATCHUP_DAYS", 7)


def max_attempts() -> int:
    return _setting("HOMEOA_SCHEDULER_MAX_ATTEMPTS", 5)


def stale_after() -> dt.timedelta:
    return dt.timedelta(seconds=_setting("HOMEOA_SCHEDULER_STALE_SECONDS", 3600))


def due_dates(job: PeriodicJob, assoc: Association, now: dt.datetime, model) -> list[dt.date]:
    """Dates the worker should run ``job`` for: missed dates (catch-up) up to today."""
    tz = ZoneInfo(assoc.timezone)
    local_now = now.astimezone(tz)
    today = local_now.date()
    start = today - dt.timedelta(days=catchup_days())
    if assoc.created_at:
        start = max(start, assoc.created_at.astimezone(tz).date())
    last_ok = (
        model.all_associations.filter(
            association=assoc, job_name=job.name, status=model.Status.SUCCEEDED
        )
        .order_by("-as_of")
        .values_list("as_of", flat=True)
        .first()
    )
    if last_ok is not None:
        start = max(start, last_ok + dt.timedelta(days=1))
    # A module's job does not make up for days it was off: catch-up starts on the day the
    # module was last turned on.
    floor = today - dt.timedelta(days=catchup_days())
    if job.module is not None:
        from homeoa.plugins.modules import turned_on_at

        since = turned_on_at(assoc, job.module)
        if since is not None:
            floor = max(floor, since.astimezone(tz).date())
            start = max(start, floor)
    dates = []
    day = start
    while day <= today:
        times = job.fire_times(day, tz)
        if times and times[0] <= local_now:
            dates.append(day)
        day += dt.timedelta(days=1)
    if not job.catch_up:
        dates = dates[-1:]
    dates = [d for d in dates if runs_on(job, assoc, d)]
    # Failed dates inside the window are retried even after a later date succeeded.
    failed = model.all_associations.filter(
        association=assoc,
        job_name=job.name,
        status=model.Status.FAILED,
        attempts__lt=max_attempts(),
        as_of__gte=floor,
        as_of__lte=today,
    ).values_list("as_of", flat=True)
    failed = [d for d in failed if runs_on(job, assoc, d)]
    return sorted(set(dates) | set(failed))


def run_due(
    *,
    as_of: dt.date | None = None,
    now: dt.datetime | None = None,
    association: Association | None = None,
    names: Iterable[str] | None = None,
    rerun: bool = False,
) -> list:
    """Run due periodic jobs synchronously and return the ``ScheduledRun`` rows touched.

    With ``as_of``, every job that fires on that date runs, whatever the time of day, and
    nothing else (no catch-up). Otherwise (worker tick) each association's missed and
    current dates are run in date order, from the later of the last successful run and
    ``now - HOMEOA_SCHEDULER_CATCHUP_DAYS`` (default 7), never before the association was
    created. Successful runs are skipped unless ``rerun``; runs another process has in
    progress are skipped until ``HOMEOA_SCHEDULER_STALE_SECONDS`` (default 3600) pass;
    failed runs are retried up to ``HOMEOA_SCHEDULER_MAX_ATTEMPTS`` (default 5) attempts,
    and hitting the cap publishes ``jobs.run_failed``. Jobs that wait for go-live skip every
    date before the association's go-live date in all of these paths.
    """
    from .models import ScheduledRun

    now = now or timezone.now()
    wanted = set(names) if names is not None else None
    results = []
    for assoc in _associations(association):
        tz = ZoneInfo(assoc.timezone)
        for job in daily_jobs(assoc):
            if wanted is not None and job.name not in wanted:
                continue
            if as_of is not None:
                days = [as_of] if job.is_due(as_of, tz) else []
            else:
                days = due_dates(job, assoc, now, ScheduledRun)
            for day in days:
                run = _execute(job, assoc, day, rerun, ScheduledRun)
                if run is not None:
                    results.append(run)
    return results


def _execute(job: PeriodicJob, assoc: Association, day: dt.date, rerun: bool, model):
    if not runs_on(job, assoc, day):
        return None  # before go-live: skipped for good, no run recorded
    S = model.Status
    with transaction.atomic():
        run, _created = model.all_associations.select_for_update().get_or_create(
            association=assoc, job_name=job.name, as_of=day
        )
        if run.status == S.SUCCEEDED and not rerun:
            return None
        if (
            run.status == S.RUNNING
            and run.started_at is not None
            and run.started_at > timezone.now() - stale_after()
        ):
            return None  # another worker (or run_scheduled) has it
        if run.status == S.FAILED and run.attempts >= max_attempts() and not rerun:
            return None
        run.status = S.RUNNING
        run.started_at = timezone.now()
        run.finished_at = None
        run.error = ""
        run.attempts += 1
        run.save()
    try:
        with (
            transaction.atomic(),
            association_context(assoc),
            audit_context(label=f"job:{job.name}"),
        ):
            job.func(assoc, day)
    except Exception:
        logger.exception("Periodic job %s failed for association %s on %s", job.name, assoc.pk, day)
        run.status = S.FAILED
        run.error = traceback.format_exc()[-4000:]
    else:
        run.status = S.SUCCEEDED
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error", "finished_at"])
    if run.status == S.FAILED and run.attempts >= max_attempts():
        from .events import JobRunFailed

        publish(
            JobRunFailed(
                association_id=assoc.pk,
                job_name=job.name,
                as_of=day,
                attempts=run.attempts,
                run_id=run.pk,
            )
        )
    return run
