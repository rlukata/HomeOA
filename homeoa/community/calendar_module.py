"""Module manifest for calendar; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="calendar",
    name=_("Calendar"),
    version=CORE_VERSION,
    description=_("The association calendar."),
    kind="module",
    code=("homeoa/community/calendar_module.py",),
    ready="homeoa.community.calendar_module.ready",
    pages=(
        "community:calendar",
        "community:event_*",
        "community:feed*",
        "community:ical",
        "community:resident",
    ),
)


def ready():
    """Add the module's board menu sections and Home contributions."""
    from homeoa.plugins import slots
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("community", SubTab("calendar", _("Calendar"), "community:calendar"), -10),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.HOME_CARDS, "community/_home_calendar_card.html", module=MANIFEST.id, order=10
    )
