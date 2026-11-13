"""Cards other pages include without view changes: resident Home and board Home."""

from django import template

from homeoa.core.context import association_today

from .. import services

register = template.Library()

PORTAL_LIMIT = 3
HOME_LIMIT = 5


def _association(context):
    request = context.get("request")
    return request, getattr(request, "association", None)


@register.inclusion_tag("community/_portal_card.html", takes_context=True)
def portal_announcements(context):
    """Live announcements for the resident Home. The board's preview-as-unit
    (``read_only``) shows what that unit's owner (or tenant) would see."""
    request, association = _association(context)
    if association is None:
        return {"announcements": []}
    if context.get("read_only"):
        audiences = services.unit_audiences(bool(context.get("show_money")))
    else:
        audiences = services.audiences_for(request.user, association)
    rows = list(services.visible_announcements(association, audiences)[:PORTAL_LIMIT])
    return {"announcements": rows, "read_only": context.get("read_only")}


@register.inclusion_tag("community/_home_calendar.html", takes_context=True)
def home_calendar(context):
    request, association = _association(context)
    if association is None:
        return {"events": [], "show": False}
    audiences = services.audiences_for(request.user, association)
    today = association_today(association)
    return {
        "events": services.upcoming(association, audiences, today, limit=HOME_LIMIT),
        "show": bool(audiences),
    }
