"""Periodic jobs owned by core (autodiscovered by procrastinate)."""

from homeoa.jobs import periodic

from .ops import check_backups


@periodic("ops.backup_check", cron="0 8 * * *", catch_up=False)
def backup_check(association, as_of):
    """Alert ADMINS and publish ops.backup_stale when backups are old or failing.

    Staleness is relative to now, not ``as_of``: it reports the current state."""
    check_backups(association)
