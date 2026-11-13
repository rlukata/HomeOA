"""Module manifest for board packet; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="board_packet",
    name=_("Board packet"),
    version=CORE_VERSION,
    description=_("The monthly board packet."),
    kind="module",
    code=("homeoa/reports/packet*.py",),
    ready="homeoa.reports.packet_module.ready",
    pages=("reports:board_packet",),
)


def ready():
    """Register the packet's notification type and add its own sections; build and send
    the packet once a month close commits."""
    from homeoa import events
    from homeoa.plugins import slots
    from homeoa.reports.packet import SECTIONS, register_notification_type

    register_notification_type()

    for build, order in SECTIONS:
        slots.register(slots.BOARD_PACKET_SECTIONS, build, module=MANIFEST.id, order=order)

    events.handle("closing.month_closed", on_month_closed, "on_commit", module=MANIFEST.id)


def on_month_closed(event):
    from homeoa.accounts.models import User
    from homeoa.core.models import Association
    from homeoa.reports import packet

    association = Association.objects.get(pk=event.association_id)
    user = User.objects.filter(pk=event.closed_by_id).first() if event.closed_by_id else None
    packet.after_close(association, event.year, event.month, user=user)
