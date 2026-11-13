"""Module manifest for design approval; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="arc",
    name=_("Design approval"),
    version=CORE_VERSION,
    description=_("Architectural (design approval) requests and decisions."),
    kind="module",
    code=("homeoa/arc",),
    ready="homeoa.arc.module.ready",
    requires=("committees",),
    pages=("arc:*",),
)


def ready():
    """Add the module's board menu sections, Home and owner portal contributions, and its
    reviewers as office holders."""
    from homeoa.arc.apps import can_review
    from homeoa.plugins import slots
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("requests", SubTab("architectural", _("Architectural"), "arc:index", can_review), 20),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.HOME_TODOS, ("arc", "homeoa.arc.todos.todo_items"), module=MANIFEST.id, order=205
    )

    from homeoa.arc.portal import REQUEST_KIND

    slots.register(slots.OWNER_PORTAL, REQUEST_KIND, module=MANIFEST.id, order=20)

    from homeoa.arc.services import reviewers

    slots.register(slots.OFFICE_HOLDERS, reviewers, module=MANIFEST.id)

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)

    from homeoa.arc.search import design_requests

    slots.register(slots.SEARCH_SOURCES, design_requests, module=MANIFEST.id, order=85)


def open_items(association):
    """Design approval requests still waiting for a decision."""
    from homeoa.arc.models import ArcRequest
    from homeoa.plugins.slots import OpenItem

    rows = ArcRequest.all_associations.filter(
        association=association, status__in=ArcRequest.OPEN
    ).select_related("unit")
    return [
        OpenItem(
            _("Design approval request: %(title)s, unit %(unit)s")
            % {"title": r.title, "unit": r.unit.number},
            "arc:detail",
            (r.pk,),
        )
        for r in rows
    ]
