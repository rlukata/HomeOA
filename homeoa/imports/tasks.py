"""Periodic jobs for imports."""

from homeoa.jobs import periodic


@periodic("imports.purge_files", cron="0 3 * * *")
def purge_files(association, as_of):
    """Purge raw files of committed imports older than the retention period (REG-05)."""
    from .retention import purge_expired

    purge_expired(association, as_of)
