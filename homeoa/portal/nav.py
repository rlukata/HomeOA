"""Tab registry for the resident app under ``/me/`` (owner.html: Home/Pay/Requests/Docs/More).

Like ``ui.nav`` for the board shell: apps that build a tab's real page later call
``register_tab`` with the same id to replace it. A tab with ``permission`` set shows only
to users who hold that code (tenants have no ``balance.own``, so no Pay tab).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm


@dataclass(frozen=True)
class Tab:
    id: str
    label: str
    icon: str
    order: int
    url_name: str
    permission: str | None = None

    @property
    def url(self) -> str:
        return reverse(self.url_name)

    def is_visible(self, request) -> bool:
        if self.permission is None:
            return True
        return has_perm(
            request.user, self.permission, association=getattr(request, "association", None)
        )


_registry: dict[str, Tab] = {}


def register_tab(tab: Tab) -> Tab:
    _registry[tab.id] = tab
    return tab


def get_tab(tab_id: str) -> Tab | None:
    return _registry.get(tab_id)


def get_tabs(request) -> list[Tab]:
    return [t for t in sorted(_registry.values(), key=lambda t: t.order) if t.is_visible(request)]


for _tab in (
    Tab("home", _("Home"), "home", 10, "portal:home"),
    Tab("pay", _("Pay"), "money", 20, "portal:pay", perms.BALANCE_OWN),
    Tab("requests", _("Requests"), "wrench", 30, "portal:requests"),
    Tab("docs", _("Docs"), "doc", 40, "portal:docs"),
    Tab("more", _("More"), "menu", 50, "portal:more"),
):
    register_tab(_tab)
