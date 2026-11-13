"""Module manifest for work orders; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="work_orders",
    name=_("Work orders"),
    version=CORE_VERSION,
    description=_("Work orders, preventive tasks and building components."),
    kind="module",
    code=(
        "homeoa/maintenance/work_orders_module.py",
        "homeoa/maintenance/upkeep*.py",
        "homeoa/maintenance/tasks.py",
        "homeoa/maintenance/packet.py",
        "homeoa/maintenance/work_orders_search.py",
    ),
    ready="homeoa.maintenance.work_orders_module.ready",
    pages=(
        "maintenance:index",
        "maintenance:work_order*",
        "maintenance:link_bill",
        "maintenance:unlink_bill",
        "maintenance:task*",
        "maintenance:component*",
    ),
)


def ready():
    """Add the module's board menu sections, Home, setup, board packet and compliance
    calendar contributions."""
    from homeoa.maintenance.apps import _can_manage
    from homeoa.plugins import slots
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("requests", SubTab("preventive", _("Preventive"), "maintenance:tasks", _can_manage), 1000),
        (
            "requests",
            SubTab("components", _("Components"), "maintenance:components", _can_manage),
            1010,
        ),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.HOME_TODOS,
        ("work_orders", "homeoa.maintenance.services.work_order_todo_items"),
        module=MANIFEST.id,
        order=215,
    )

    from homeoa.maintenance.oneoff import apply_setup_answer

    slots.register(
        slots.WIZARD_SECTIONS,
        ("maintenance.oneoff_limit", apply_setup_answer),
        module=MANIFEST.id,
    )

    from homeoa.maintenance.packet import open_work_orders

    slots.register(slots.BOARD_PACKET_SECTIONS, open_work_orders, module=MANIFEST.id, order=70)

    from homeoa.maintenance.upkeep import compliance_entries

    slots.register(slots.COMPLIANCE_CALENDAR_ENTRIES, compliance_entries, module=MANIFEST.id)

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)

    from homeoa.maintenance.work_orders_search import work_orders

    slots.register(slots.SEARCH_SOURCES, work_orders, module=MANIFEST.id, order=75)


def open_items(association):
    """Work orders not yet completed or cancelled. Preventive task schedules are not items."""
    from homeoa.maintenance.models import WorkOrder
    from homeoa.plugins.slots import OpenItem

    rows = WorkOrder.all_associations.filter(
        association=association, status__in=WorkOrder.OPEN
    ).select_related("vendor")
    return [
        OpenItem(
            _("Work order %(n)s: %(vendor)s") % {"n": w.pk, "vendor": w.vendor.name},
            "maintenance:work_order",
            (w.pk,),
        )
        for w in rows
    ]
