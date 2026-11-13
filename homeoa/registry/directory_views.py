"""Resident directory pages: Community > Resident directory for the board, More > Resident
directory for owners."""

from __future__ import annotations

from django.shortcuts import render

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.core.context import association_today

from . import directory


def _units(request):
    return directory.units(request.association, association_today(request.association))


@require_perm(perms.BOARD_SHELL)
@require_perm(perms.DIRECTORY_READ)
def board(request):
    return render(request, "registry/directory.html", {"units": _units(request)})


@resident_view(perm=perms.DIRECTORY_READ)
def portal(request):
    from homeoa.portal.views.base import _render

    return _render(request, "more", "registry/portal_directory.html", units=_units(request))
