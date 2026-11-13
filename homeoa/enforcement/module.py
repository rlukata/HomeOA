"""Module manifest for violations; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="violations",
    name=_("Violations"),
    version=CORE_VERSION,
    description=_("Violation notices, hearings and fines."),
    kind="module",
    code=("homeoa/enforcement",),
    ready="homeoa.enforcement.module.ready",
    pages=("enforcement:*", "violations_portal:*"),
)

# The resale certificate's violations item while this module is off.
VIOLATIONS_NOT_TRACKED = _(
    "The association does not track violations in HomeOA. The board must answer this item."
)


def ready():
    """Add the module's board menu sections, Home, owner portal, setup, resale certificate
    and board packet contributions."""
    from homeoa.enforcement.apps import _can_manage
    from homeoa.plugins import slots
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("requests", SubTab("violations", _("Violations"), "enforcement:index", _can_manage), 30),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.HOME_TODOS,
        ("enforcement", "homeoa.enforcement.todos.todo_items"),
        module=MANIFEST.id,
        order=165,
    )

    from homeoa.enforcement.portal import REQUESTS_PART

    slots.register(slots.OWNER_PORTAL, REQUESTS_PART, module=MANIFEST.id)

    from homeoa.enforcement.setup_answers import apply_fines

    slots.register(slots.WIZARD_SECTIONS, ("fines.", apply_fines), module=MANIFEST.id)

    from homeoa.enforcement.packet import open_violations
    from homeoa.enforcement.resale import violations_answer

    slots.register(
        slots.RESALE_CERTIFICATE_ANSWERS,
        ("violations", violations_answer, VIOLATIONS_NOT_TRACKED),
        module=MANIFEST.id,
    )
    slots.register(slots.BOARD_PACKET_SECTIONS, open_violations, module=MANIFEST.id, order=80)

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)

    from homeoa.enforcement.search import violations

    slots.register(slots.SEARCH_SOURCES, violations, module=MANIFEST.id, order=80)


def open_items(association):
    """Open violations: cured, closed and dismissed ones are finished."""
    from django.utils.formats import date_format

    from homeoa.enforcement.models import Violation
    from homeoa.plugins.slots import OpenItem

    rows = Violation.all_associations.filter(
        association=association, status=Violation.Status.OPEN
    ).select_related("unit", "category")
    return [
        OpenItem(
            _("Violation: %(category)s at unit %(unit)s, observed on %(date)s")
            % {"category": v.category, "unit": v.unit.number, "date": date_format(v.observed_on)},
            "enforcement:detail",
            (v.pk,),
        )
        for v in rows
    ]
