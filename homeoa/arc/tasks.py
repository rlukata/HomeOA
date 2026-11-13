"""Periodic jobs for architectural review."""

from homeoa.jobs import periodic


@periodic("arc.deadline_reminders", cron="0 8 * * *", module="arc")
def deadline_reminders(association, as_of):
    """Remind reviewers of architectural decision deadlines (idempotent)."""
    from .deadlines import send_reminders

    send_reminders(association, as_of)


@periodic("arc.deemed_approvals", cron="0 7 * * *", module="arc")
def deemed_approvals(association, as_of):
    """Record EV charger and heat pump requests the law now deems approved (idempotent)."""
    from .deemed import record_deemed_approvals

    record_deemed_approvals(association, as_of)
