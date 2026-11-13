from django import template

from homeoa.notify.inbox import unread_count

register = template.Library()


@register.inclusion_tag("notify/_bell.html", takes_context=True)
def notify_bell(context):
    """Header bell linking to /notifications/ with the unread count (hidden when gated)."""
    request = context.get("request")
    return {"count": unread_count(request) if request is not None else None}
