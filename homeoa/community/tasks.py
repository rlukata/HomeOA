"""Periodic jobs for community."""

from homeoa.jobs import periodic


@periodic("community.announcements_due", cron="0 7 * * *", module="announcements")
def announcements_due(association, as_of):
    """Send "also notify" announcements whose publish day has come (idempotent)."""
    from .services import send_due

    send_due(association, as_of)
