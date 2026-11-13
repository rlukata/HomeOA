"""Procrastinate tasks owned by the jobs app (autodiscovered by procrastinate)."""

from procrastinate.contrib.django import app

from homeoa.audit import audit_context
from homeoa.core.context import fixed_today

from .scheduler import run_due


@app.periodic(cron="*/5 * * * *", periodic_id="homeoa.jobs.tick")
@app.task(name="homeoa.jobs.tick", queue="scheduler", queueing_lock="homeoa.jobs.tick")
def tick(timestamp: int) -> None:
    """Run every HomeOA periodic job that is due now, per association local time.

    Does nothing while the date is pinned with ``HOMEOA_FIXED_TODAY`` (tests, E2E, demos):
    the wall clock then says nothing about which jobs are due, and catching up to it would
    run jobs for dates the pinned app has not reached. Use ``run_scheduled --as-of`` there.
    """
    if fixed_today() is not None:
        return
    with audit_context(label="job:homeoa.jobs.tick"):
        run_due()
