"""Periodic jobs for maintenance."""

from homeoa.jobs import periodic


@periodic("maintenance.preventive", cron="30 6 * * *", module="work_orders")
def preventive(association, as_of):
    """Open draft work orders for preventive tasks that are due soon (idempotent)."""
    from .upkeep import open_due

    open_due(association, as_of)
