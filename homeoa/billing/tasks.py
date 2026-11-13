"""Periodic jobs for billing."""

from homeoa.jobs import periodic

from .services import post_due_assessments


@periodic("billing.post_assessments", cron="0 6 * * *", waits_for_go_live=True)
def post_assessments(association, as_of):
    """Post monthly assessments once each schedule's posting day is reached (idempotent).
    A month whose dues fall due before go-live is not posted."""
    from homeoa.core import status

    post_due_assessments(association, as_of, not_before=status.go_live_date(association))


@periodic("billing.assess_late_fees", cron="0 7 * * *", waits_for_go_live=True)
def assess_late_fees(association, as_of):
    """Assess late fees and monthly interest under each active policy (idempotent). Charges
    due before go-live get none: the policies are treated as starting on the go-live date."""
    from homeoa.core import status

    from .services.latefees import assess_due

    assess_due(association, as_of, not_before=status.go_live_date(association))


@periodic("billing.post_special_installments", cron="0 6 * * *", waits_for_go_live=True)
def post_special_installments(association, as_of):
    """Post special assessment installments whose charge date has been reached. Installments
    dated before go-live are left for the board to post by hand."""
    from homeoa.core import status

    from .services.special import post_due_installments

    post_due_installments(association, as_of, since=status.go_live_date(association))
