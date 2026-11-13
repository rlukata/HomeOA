"""Module manifest for resident directory; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="directory",
    name=_("Resident directory"),
    version=CORE_VERSION,
    description=_("The opt-in resident directory."),
    kind="module",
    code=("homeoa/registry/directory*.py",),
    ready="homeoa.registry.directory_module.ready",
    pages=("directory:*",),
)


def ready():
    """Add the module's board menu sections and owner portal contributions."""
    from homeoa.plugins import slots
    from homeoa.portal.contributions import PortalPart
    from homeoa.registry.apps import _can_read_directory
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        (
            "community",
            SubTab("directory", _("Resident directory"), "directory:board", _can_read_directory),
            5,
        ),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    for place, template in (
        ("more", "registry/_portal_more_link.html"),
        ("welcome_how", "registry/_portal_welcome_link.html"),
    ):
        slots.register(slots.OWNER_PORTAL, PortalPart(place, template), module=MANIFEST.id)
