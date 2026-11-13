"""``{% load homeoa_access %}`` then ``{% has_perm "units.manage" as can_edit %}``."""

from django import template

from homeoa.access.services import has_perm as _has_perm

register = template.Library()


@register.simple_tag(takes_context=True)
def has_perm(context, code, obj=None):
    request = context.get("request")
    if request is None:
        return False
    return _has_perm(request.user, code, obj, association=getattr(request, "association", None))


@register.simple_tag
def unit_invitations(unit, on):
    """``(person, invitation-or-None)`` pairs for the unit page access section."""
    from homeoa.access.invitations import unit_people

    return unit_people(unit, on)


@register.simple_tag(takes_context=True)
def residents_on(context):
    from homeoa.access.services import residents_enabled

    request = context.get("request")
    association = getattr(request, "association", None)
    return bool(association and residents_enabled(association))
