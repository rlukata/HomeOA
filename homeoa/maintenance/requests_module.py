"""Module manifest for owner requests; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="owner_requests",
    name=_("Owner requests"),
    version=CORE_VERSION,
    description=_("Maintenance requests from owners and the board."),
    kind="module",
    code=("homeoa/maintenance/requests_*.py",),
    ready="homeoa.maintenance.requests_module.ready",
    pages=(
        "maintenance:index",
        "maintenance:add",
        "maintenance:detail",
        "maintenance:request_status",
        "maintenance:add_photos",
        "maintenance:photo",
        "maintenance:my_*",
    ),
)


def ready():
    """Add the module's board menu sections, Home and owner portal contributions."""
    from homeoa.plugins import slots
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("requests", SubTab("maintenance", _("Maintenance"), "maintenance:index"), 10),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.HOME_TODOS,
        ("owner_requests", "homeoa.maintenance.services.request_todo_items"),
        module=MANIFEST.id,
        order=210,
    )

    from homeoa.maintenance.requests_portal import REQUEST_KIND

    slots.register(slots.OWNER_PORTAL, REQUEST_KIND, module=MANIFEST.id, order=10)

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)

    from homeoa.maintenance.requests_search import maintenance_requests

    slots.register(slots.SEARCH_SOURCES, maintenance_requests, module=MANIFEST.id, order=70)


def open_items(association):
    """Maintenance requests that are new or in progress."""
    from homeoa.maintenance.models import MaintenanceRequest
    from homeoa.plugins.slots import OpenItem

    rows = MaintenanceRequest.all_associations.filter(
        association=association,
        status__in=(MaintenanceRequest.Status.NEW, MaintenanceRequest.Status.IN_PROGRESS),
    )
    return [
        OpenItem(
            _("Maintenance request #%(n)s: %(title)s") % {"n": r.pk, "title": r.title},
            "maintenance:detail",
            (r.pk,),
        )
        for r in rows
    ]
