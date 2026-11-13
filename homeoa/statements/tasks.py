"""Periodic jobs for statements."""

from homeoa.jobs import periodic


@periodic("statements.delinquency_ladder", cron="0 8 * * *", waits_for_go_live=True)
def delinquency_ladder(association, as_of):
    """Advance delinquent units on the reminder / late notice / demand ladder (idempotent)."""
    from .delinquency import run

    run(association, as_of)
