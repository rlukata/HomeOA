"""Periodic jobs for payments."""

from homeoa.jobs import periodic


@periodic("payments.autopay", cron="0 9 * * *", module="online_payments", waits_for_go_live=True)
def autopay(association, as_of):
    """Start due autopay debits (after ``billing.post_assessments``; idempotent per month)."""
    from .autopay import run

    run(association, as_of)
