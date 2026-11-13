"""A new charge consumes the unit's unapplied credit (prepayments, credit memos)."""


def charge_created(sender, instance, created, raw=False, **kwargs):
    if not created or raw or instance.void_entry_id:
        return
    from .services import apply_credit

    apply_credit(
        instance.unit, date=instance.charge_date, ownership_period=instance.ownership_period
    )
