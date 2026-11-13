"""Shared rendering for resident tabs."""

from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from homeoa.access import permissions as perms
from homeoa.access.services import roles_for, units_for

from ..nav import get_tab, get_tabs


def _render(request, tab_id, template, **extra):
    tab = get_tab(tab_id)
    if "preview" not in extra and (tab is None or not tab.is_visible(request)):
        raise PermissionDenied
    association = request.association
    context = {
        "tabs": get_tabs(request),
        "current_tab": tab,
        "units": units_for(request.user, association),
        "is_tenant_only": perms.OWNER not in roles_for(request.user, association),
        **extra,
    }
    return render(request, template, context)
