"""Module manifest for online payments; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="online_payments",
    name=_("Online payments"),
    version=CORE_VERSION,
    description=_("Online payment and autopay for owners."),
    kind="module",
    code=("homeoa/payments/autopay*.py", "homeoa/payments/tasks.py"),
    ready="homeoa.payments.autopay_module.ready",
    pages=("portal:pay_confirm", "portal:autopay", "payments:autopay_*"),
)


def ready():
    """Register autopay's notification types; add the module's Home to-dos, its parts of the
    owner's Pay page and the next debit on owner statements."""
    from homeoa.payments.autopay import register_notification_types
    from homeoa.plugins import slots

    register_notification_types()

    slots.register(
        slots.HOME_TODOS,
        ("autopay", "homeoa.payments.autopay.todo_items"),
        module=MANIFEST.id,
        order=55,
    )

    from homeoa.payments.autopay_portal import PARTS

    for part in PARTS:
        slots.register(slots.OWNER_PORTAL, part, module=MANIFEST.id)

    from homeoa.payments.autopay import statement_line

    slots.register(slots.STATEMENT_EXTRAS, statement_line, module=MANIFEST.id)

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)


# The ACH return window: a debit can still come back this many banking days after it is sent.
ACH_RETURN_BANKING_DAYS = 10


def _banking_days_before(day, n):
    """The date ``n`` weekdays before ``day`` (no holiday calendar)."""
    import datetime as dt

    while n:
        day -= dt.timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def open_items(association):
    """Units whose autopay is still on, or that have an autopay debit scheduled, or sent
    and still inside the ACH return window."""
    from django.db.models import Exists, OuterRef, Q

    from homeoa.core.context import association_today
    from homeoa.payments.models import AutopayCharge, AutopayEnrollment, AutopayStatus
    from homeoa.plugins.slots import OpenItem
    from homeoa.registry.models import Unit

    enrolled = AutopayEnrollment.all_associations.filter(
        unit=OuterRef("pk"), cancelled_at__isnull=True
    )
    # The debit is sent on its debit date (older rows without one fall back to creation).
    cutoff = _banking_days_before(association_today(association), ACH_RETURN_BANKING_DAYS)
    scheduled = AutopayCharge.all_associations.filter(
        unit=OuterRef("pk"), status=AutopayStatus.SCHEDULED
    )
    processing = AutopayCharge.all_associations.filter(
        unit=OuterRef("pk"), status=AutopayStatus.PENDING
    ).filter(Q(debit_on__gte=cutoff) | Q(debit_on__isnull=True, created_at__date__gte=cutoff))
    rows = (
        Unit.all_associations.filter(association=association)
        .annotate(
            enrolled=Exists(enrolled),
            scheduled=Exists(scheduled),
            processing=Exists(processing),
        )
        .filter(Q(enrolled=True) | Q(scheduled=True) | Q(processing=True))
    )
    items = []
    for u in rows:
        if u.enrolled:
            items.append(OpenItem(_("Autopay is on for unit %(unit)s") % {"unit": u.number}))
        if u.scheduled:
            items.append(
                OpenItem(_("Autopay debit not yet taken for unit %(unit)s") % {"unit": u.number})
            )
        if u.processing:
            items.append(
                OpenItem(
                    _("Autopay debit for unit %(unit)s may still be returned by the bank")
                    % {"unit": u.number}
                )
            )
    return items
