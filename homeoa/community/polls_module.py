"""Module manifest for polls; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="polls",
    name=_("Polls"),
    version=CORE_VERSION,
    description=_("Non-binding owner polls."),
    kind="module",
    code=(
        "homeoa/community/polls*.py",
        "homeoa/community/poll_views.py",
        "homeoa/community/templatetags/poll_tags.py",
    ),
    ready="homeoa.community.polls_module.ready",
    pages=(
        "community:polls",
        "community:poll",
        "community:poll_close",
        "community:resident_poll",
    ),
)


def ready():
    """Add the module's board menu sections and owner portal contributions."""
    from homeoa.community.apps import _can_manage
    from homeoa.plugins import slots
    from homeoa.portal.contributions import PortalPart
    from homeoa.ui.nav import SubTab, register_subtab

    for hub, tab, order in (
        ("community", SubTab("polls", _("Polls"), "community:polls", _can_manage), 1000),
    ):
        register_subtab(hub, tab, module=MANIFEST.id, order=order)

    slots.register(
        slots.OWNER_PORTAL,
        PortalPart("home", "community/_portal_home_polls.html"),
        module=MANIFEST.id,
        order=10,
    )

    slots.register(slots.OPEN_ITEMS_CHECKS, open_items, module=MANIFEST.id)


def open_items(association):
    """Polls open today or scheduled to open later; closed ones are finished."""
    from django.utils.formats import date_format

    from homeoa.community.models import Poll
    from homeoa.core.context import association_today
    from homeoa.plugins.slots import OpenItem

    today = association_today(association)
    rows = Poll.all_associations.filter(
        association=association,
        closed_early_on__isnull=True,
        closes_on__gte=today,
    )
    return [
        OpenItem(
            _("Poll: %(question)s, closes on %(date)s")
            % {"question": p.question, "date": date_format(p.closes_on)},
            "community:poll",
            (p.pk,),
        )
        for p in rows
    ]
