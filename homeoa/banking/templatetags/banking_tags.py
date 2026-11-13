from zoneinfo import ZoneInfo

from django import template
from django.utils import dateformat, timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _

from homeoa.core.context import association_today

register = template.Library()


def synced_text(value, association=None) -> str:
    """Sync time as "9:12 am" today or "9:12 am on Oct 6", in the association time zone."""
    try:
        tz = ZoneInfo(association.timezone) if association else timezone.get_current_timezone()
    except Exception:
        tz = timezone.get_current_timezone()
    local = value.astimezone(tz)
    clock = dateformat.format(local, "g:i A").lower()
    if local.date() == association_today(association):
        return clock
    return _("%(time)s on %(date)s") % {"time": clock, "date": dateformat.format(local, "M j")}


@register.simple_tag(takes_context=True)
def synced_at(context, value):
    """``{% synced_at account.last_synced_at %}``: a ``<time>`` with the local sync time."""
    if not value:
        return ""
    request = context.get("request")
    text = synced_text(value, getattr(request, "association", None))
    return format_html('<time datetime="{}">{}</time>', value.isoformat(), text)
