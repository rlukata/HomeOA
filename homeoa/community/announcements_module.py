"""Module manifest for announcements; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="announcements",
    name=_("Announcements"),
    version=CORE_VERSION,
    description=_("Announcements to owners and residents."),
    kind="module",
    code=("homeoa/community/announcements_module.py",),
    ready="homeoa.community.announcements_module.ready",
    pages=("community:index", "community:announcement*", "community:resident"),
)


def ready():
    """Add the module's board menu sections and owner portal contributions."""
    from homeoa.plugins import slots
    from homeoa.portal.contributions import PortalPart
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("community", SubTab("announcements", _("Announcements"), "community:announcements"), -20),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.OWNER_PORTAL,
        PortalPart("home", "community/_portal_home_announcements.html"),
        module=MANIFEST.id,
        order=20,
    )

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)


def open_items(association):
    """Announcements scheduled for a later day, or whose notification has not gone out."""
    from django.db.models import Q
    from django.utils.formats import date_format

    from homeoa.community.models import Announcement
    from homeoa.core.context import association_today
    from homeoa.plugins.slots import OpenItem

    today = association_today(association)
    rows = Announcement.all_associations.filter(association=association).filter(
        Q(publish_on__gt=today)
        | (
            Q(also_notify=True, notified_on__isnull=True)
            & (Q(expires_on__isnull=True) | Q(expires_on__gte=today))
        )
    )
    return [
        OpenItem(
            _("Announcement: %(title)s, publishes on %(date)s")
            % {"title": a.title, "date": date_format(a.publish_on)},
            "community:announcement_edit",
            (a.pk,),
        )
        for a in rows
    ]
