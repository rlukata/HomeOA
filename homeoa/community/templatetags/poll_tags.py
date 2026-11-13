"""The open polls card on the resident Home."""

from django import template

from homeoa.core.context import association_today

from .community_tags import _association

register = template.Library()


@register.inclusion_tag("community/_poll_card.html", takes_context=True)
def portal_polls(context):
    """Open polls the signed-in resident can answer, for the resident Home."""
    from ..polls import open_polls

    request, association = _association(context)
    if association is None or context.get("read_only"):
        return {"polls": []}
    return {"polls": open_polls(request.user, association, association_today(association))}
