"""Periodic jobs for compliance obligations."""

from homeoa.jobs import periodic


@periodic("compliance.open_periods", cron="0 7 * * *")
def open_periods(association, as_of):
    """Open each obligation's period when due and flag missed deadlines (idempotent)."""
    from .services import run

    run(association, as_of)


@periodic("compliance.reminders", cron="15 7 * * *")
def reminders(association, as_of):
    """Remind the board before each compliance deadline (idempotent per item and offset)."""
    from .calendar import send_reminders

    send_reminders(association, as_of)
