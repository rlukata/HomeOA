"""Board preview-as-unit (POR-01): ``/units/<n>/preview/``.

A board member with ``units.read`` sees the resident pages of one unit exactly as its
owner (default) or tenant (``?as=tenant``) would, read-only: a persistent banner, no Pay
action, and every non-GET request refused (405). Works whether or not residents are
switched on, so the board can check the portal before cutover. Each preview
session (unit + persona) is audited once per login session.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.views.decorators.http import require_safe

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm

from .nav import _registry as _tab_registry
from .nav import get_tab
from .views.home import render_home

OWNER = "owner"
TENANT = "tenant"
_SESSION_KEY = "homeoa_preview_audited"


@dataclass(frozen=True)
class PreviewTab:
    """Looks like ``nav.Tab`` to the templates, but links inside the preview."""

    id: str
    label: str
    icon: str
    url: str


def _persona(request) -> str:
    return TENANT if request.GET.get("as") == TENANT else OWNER


def _tabs(unit, persona) -> list[PreviewTab]:
    query = "?as=tenant" if persona == TENANT else ""
    out = []
    for tab in sorted(_tab_registry.values(), key=lambda t: t.order):
        if tab.permission == perms.BALANCE_OWN and persona == TENANT:
            continue  # tenants have no money tabs
        url = reverse("portal_preview:tab", args=[unit.number, tab.id])
        if tab.id == "home":
            url = reverse("portal_preview:home", args=[unit.number])
        out.append(PreviewTab(tab.id, str(tab.label), tab.icon, url + query))
    return out


def _audit_once(request, unit, persona) -> None:
    seen = request.session.get(_SESSION_KEY, [])
    key = f"{unit.pk}:{persona}"
    if key in seen:
        return
    audit.record("unit.preview", unit, changes={"persona": persona})
    request.session[_SESSION_KEY] = [*seen, key]


def _context(unit, persona, tabs, current):
    return {
        "tabs": tabs,
        "current_tab": current,
        "preview": {
            "unit": unit,
            "persona": persona,
            "exit_url": reverse("registry:unit", args=[unit.number]),
        },
        "is_tenant_only": persona == TENANT,
    }


def _unit(number):
    from homeoa.registry.models import Unit

    return get_object_or_404(Unit.objects, number=number)


@require_safe
@require_perm(perms.UNITS_READ)
def home(request, number):
    unit = _unit(number)
    persona = _persona(request)
    _audit_once(request, unit, persona)
    tabs = _tabs(unit, persona)
    return render_home(
        request,
        unit=unit,
        read_only=True,
        show_money=persona == OWNER,
        extra=_context(unit, persona, tabs, tabs[0]),
    )


@require_safe
@require_perm(perms.UNITS_READ)
def tab(request, number, tab_id):
    unit = _unit(number)
    persona = _persona(request)
    tabs = _tabs(unit, persona)
    current = next((t for t in tabs if t.id == tab_id), None)
    if current is None or get_tab(tab_id) is None:
        raise Http404
    _audit_once(request, unit, persona)
    context = {"unit": unit, "read_only": True, **_context(unit, persona, tabs, current)}
    if tab_id == "requests":
        from .requests import for_unit

        context.update(for_unit(unit, owner=persona == OWNER))
        return render(request, "portal/requests.html", context)
    return render(request, "portal/tab.html", context)


urlpatterns = [
    path("", home, name="home"),
    path("<slug:tab_id>/", tab, name="tab"),
]

urls = (urlpatterns, "portal_preview")
"""Mount with ``path("units/<str:number>/preview/", include(preview.urls))``."""

__all__ = ["home", "tab", "urls"]
