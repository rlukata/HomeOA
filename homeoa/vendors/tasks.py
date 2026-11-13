"""Periodic jobs for vendors."""

from homeoa.jobs import periodic


@periodic("vendors.expiry_alerts", cron="0 7 * * *")
def expiry_alerts(association, as_of):
    """Raise certificate-expiry and contract auto-renew to-dos (idempotent)."""
    from .alerts import run

    run(association, as_of)
