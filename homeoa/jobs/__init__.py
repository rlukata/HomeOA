"""Background jobs and the association-aware scheduler (PLT-07).

Built on Procrastinate (Postgres queue, no Redis), wired to Django's database through
``procrastinate.contrib.django``. Any app's ``tasks.py`` module is autodiscovered.

One-off jobs
------------
Define a Procrastinate task and defer it; deferring inside a transaction is atomic with it::

    from homeoa.jobs import app

    @app.task(name="billing.send_statement", queue="default")
    def send_statement(association_id: int, unit_id: int) -> None: ...

    send_statement.defer(association_id=1, unit_id=4)

Periodic jobs
-------------
Register with :func:`homeoa.jobs.periodic` (usually in an app's ``tasks.py``)::

    from homeoa.jobs import periodic

    @periodic("billing.late_fees", cron="0 6 * * *")
    def assess_late_fees(association, as_of):
        ...

The job runs once per association per local date on which ``cron`` fires, with the cron
read in the association's own timezone. It receives the ``Association`` and ``as_of`` (a
``date``); job logic must use ``as_of`` and never the wall clock. Each run is recorded in
:class:`homeoa.jobs.models.ScheduledRun` (unique per association, job and date), so a run
that succeeded is not repeated. The run happens in a transaction with the association set
as the current association; an exception rolls the job's writes back and marks the run
failed (it is retried on the next tick).

Who runs them
-------------
* The worker (``manage.py run_worker``) runs ``homeoa.jobs.tick`` every few minutes, which
  runs every job whose first fire time today (local) has passed.
* ``manage.py run_scheduled --as-of YYYY-MM-DD`` runs every job due on that date
  synchronously, regardless of time of day (E2E, catch-up, ops).

Tests
-----
``homeoa.jobs.testing.capture_jobs()`` swaps in an in-memory queue and runs deferred jobs
synchronously in the test's thread and transaction.
"""

from procrastinate.contrib.django import app

from .scheduler import PeriodicJob, periodic, periodic_jobs, run_due

__all__ = ["PeriodicJob", "app", "periodic", "periodic_jobs", "run_due"]
