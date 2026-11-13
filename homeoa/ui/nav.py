"""Hub and sub-tab registry for the board shell.

Hubs are the 8 top-level areas of the board's navigation. Apps that own a
sub-tab replace its placeholder via ``register_subtab``; every section lives in the
``MENU_TABS`` slot, so an optional module's sections come only from that module.
Visibility: a hub's own ``permission`` callable wins; otherwise the resolver installed by
``homeoa.access`` (``set_permission_resolver``) decides from its hub-to-permission map.
With no resolver installed every hub is visible.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

PermissionCheck = Callable[[object], bool]


def allow_all(request) -> bool:
    return True


# hub id -> PermissionCheck; installed by homeoa.access in AppConfig.ready().
_resolver: Callable[[str], PermissionCheck] | None = None


def set_permission_resolver(resolver: Callable[[str], PermissionCheck] | None) -> None:
    global _resolver
    _resolver = resolver


@dataclass(frozen=True)
class SubTab:
    """A hub section. ``url_name`` points at an app's real page (else the placeholder);
    ``permission`` hides the tab from requests that fail it (``None``: always shown)."""

    slug: str
    label: str
    url_name: str = ""
    permission: PermissionCheck | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Hub:
    id: str
    label: str
    icon: str
    order: int
    # Priority+ collapse tier: "" never collapses, "ovf" collapses <=1100px, "ovf2" <=960px.
    overflow: str = ""
    # Shown in the phone bottom tab bar (others go in the More sheet).
    tabbar: bool = False
    subtabs: tuple[SubTab, ...] = ()
    permission: PermissionCheck = field(default=allow_all, compare=False)

    @property
    def url(self) -> str:
        if self.id == "home":
            return reverse("dashboard:index")
        return reverse("ui:hub", args=[self.id])

    def subtab_url(self, index: int) -> str:
        if self.subtabs[index].url_name:
            return reverse(self.subtabs[index].url_name)
        if index == 0:
            return self.url
        return reverse("ui:subtab", args=[self.id, self.subtabs[index].slug])

    def is_visible(self, request) -> bool:
        if self.permission is allow_all and _resolver is not None:
            return bool(_resolver(self.id)(request))
        return bool(self.permission(request))


_registry: dict[str, Hub] = {}

# Order key for core sections added after a hub's own list; module sections pass their own.
_APPENDED_ORDER = 100


@dataclass(frozen=True)
class MenuTab:
    """A section contributed to the ``MENU_TABS`` slot: ``subtab`` shown on hub ``hub``."""

    hub: str
    subtab: SubTab


def register_hub(hub: Hub) -> Hub:
    """Add a hub; the sections it lists are contributed to the menu slot as core."""
    import dataclasses

    _registry[hub.id] = dataclasses.replace(hub, subtabs=())
    for i, tab in enumerate(hub.subtabs):
        register_subtab(hub.id, tab, order=i * 10)
    return hub


def register_subtab(
    hub_id: str, subtab: SubTab, *, module: str | None = None, order: int | None = None
) -> None:
    """Contribute a section to a hub. A section with the slug of an earlier one replaces it
    in place; otherwise it goes at ``order`` (default: after the hub's own sections)."""
    from homeoa.plugins import slots

    if hub_id not in _registry:
        raise KeyError(hub_id)
    if order is None:
        order = _APPENDED_ORDER + len(_menu_tabs(hub_id))
    slots.register(slots.MENU_TABS, MenuTab(hub_id, subtab), module=module, order=order)


def _menu_tabs(hub_id: str, association=None) -> list[SubTab]:
    from homeoa.plugins import slots

    by_slug: dict[str, SubTab] = {}
    for contribution in slots.contributions(slots.MENU_TABS, association):
        if contribution.value.hub == hub_id:
            # A later section with the same slug keeps the earlier one's place.
            by_slug[contribution.value.subtab.slug] = contribution.value.subtab
    return list(by_slug.values())


def _with_subtabs(hub: Hub, association=None) -> Hub:
    import dataclasses

    return dataclasses.replace(hub, subtabs=tuple(_menu_tabs(hub.id, association)))


def hidden_subtab_urls(request) -> set[str]:
    """URLs of sections the request may not see (templates skip them)."""
    hidden = set()
    for hub in get_hubs():
        for i, tab in enumerate(hub.subtabs):
            if tab.permission is not None and not tab.permission(request):
                hidden.add(hub.subtab_url(i))
    return hidden


def get_hub(hub_id: str, association=None) -> Hub | None:
    hub = _registry.get(hub_id)
    return _with_subtabs(hub, association) if hub else None


def get_hubs(request=None) -> list[Hub]:
    """Hubs in display order, filtered by each hub's permission hook."""
    association = getattr(request, "association", None)
    hubs = [
        _with_subtabs(h, association)
        for h in sorted(_registry.values(), key=lambda h: (h.order, h.id))
    ]
    if request is None:
        return hubs
    return [h for h in hubs if h.is_visible(request)]


def _tabs(*pairs: tuple[str, str]) -> tuple[SubTab, ...]:
    return tuple(SubTab(slug, label) for slug, label in pairs)


DEFAULT_HUBS = (
    Hub("home", _("Home"), "home", 10, tabbar=True),
    Hub(
        "money",
        _("Money"),
        "money",
        20,
        tabbar=True,
        subtabs=_tabs(
            ("overview", _("Overview")),
            ("ledger", _("Dues & ledger")),
            ("bills", _("Bills")),
            ("bank", _("Bank & deposits")),
            ("budget", _("Budget")),
            ("reports", _("Reports")),
        ),
    ),
    Hub(
        "requests",
        _("Requests"),
        "wrench",
        30,
        tabbar=True,
        subtabs=_tabs(
            ("overview", _("Overview")),
            # Maintenance, Architectural and Violations come from their modules.
        ),
    ),
    Hub(
        "community",
        _("Community"),
        "megaphone",
        40,
        overflow="ovf2",
        subtabs=_tabs(
            # Announcements, Calendar and the directory come from their modules.
            ("messages", _("Messages")),
        ),
    ),
    Hub(
        "governance",
        _("Governance"),
        "users",
        50,
        overflow="ovf2",
        subtabs=_tabs(
            ("meetings", _("Meetings")),
            ("votes", _("Owner votes")),
            ("board", _("Board & committees")),
            ("compliance", _("Compliance calendar")),
        ),
    ),
    Hub(
        "units",
        _("Units"),
        "units",
        60,
        tabbar=True,
        subtabs=_tabs(
            ("all", _("All units")),
            ("people", _("Owners & tenants")),
        ),
    ),
    Hub(
        "vendors",
        _("Vendors"),
        "truck",
        70,
        overflow="ovf",
        subtabs=_tabs(
            ("all", _("All vendors")),
            # Contracts and insurance certificates live on each vendor's page.
        )
        + (SubTab("1099", _("1099 tracking"), url_name="vendors:ten99"),),
    ),
    Hub(
        "documents",
        _("Documents"),
        "doc",
        80,
        overflow="ovf",
        subtabs=_tabs(
            ("all", _("All documents")),
            ("governing", _("Governing")),
            ("financial", _("Financial")),
            ("meetings", _("Meetings")),
            ("insurance", _("Insurance")),
            ("welcome", _("Welcome packet")),
            ("records-requests", _("Records requests")),
        ),
    ),
)

for _hub in DEFAULT_HUBS:
    register_hub(_hub)
