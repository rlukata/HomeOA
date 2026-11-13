"""The board menu is built from the menu slot: core sections and module sections alike."""

from django.apps import apps
from django.test import RequestFactory

from homeoa.plugins import slots
from homeoa.ui import nav

from .test_section_tabs import board_client  # noqa: F401

BOARD_MENU = [
    ("money", "Overview", "/money/"),
    ("money", "Dues & ledger", "/money/ledger/"),
    ("money", "Bills", "/money/bills/"),
    ("money", "Bank & deposits", "/money/bank/"),
    ("money", "Budget", "/money/budget/"),
    ("money", "Reports", "/money/reports/"),
    ("money", "Delinquency", "/money/delinquency/"),
    ("money", "Close", "/money/close/"),
    ("requests", "Overview", "/requests/"),
    ("requests", "Maintenance", "/requests/maintenance/"),
    ("requests", "Architectural", "/requests/architectural/"),
    ("requests", "Violations", "/requests/violations/"),
    ("requests", "Preventive", "/requests/preventive/"),
    ("requests", "Components", "/requests/components/"),
    ("community", "Announcements", "/community/announcements/"),
    ("community", "Calendar", "/community/calendar/"),
    ("community", "Messages", "/community/messages/"),
    ("community", "Resident directory", "/community/directory/"),
    ("community", "Polls", "/community/polls/"),
    ("governance", "Meetings", "/governance/meetings/"),
    ("governance", "Owner votes", "/governance/votes/"),
    ("governance", "Board & committees", "/governance/board/"),
    ("governance", "Compliance calendar", "/governance/compliance/"),
    ("governance", "Change requests", "/governance/changes/"),
    ("governance", "Signatures", "/governance/signatures/"),
    ("units", "All units", "/units/"),
    ("units", "Owners & tenants", "/units/people/"),
    ("units", "Resale certificates", "/units/resale/"),
    ("vendors", "All vendors", "/vendors/"),
    ("vendors", "1099 tracking", "/vendors/1099/"),
    ("documents", "All documents", "/documents/"),
    ("documents", "Governing", "/documents/governing/"),
    ("documents", "Financial", "/documents/financial/"),
    ("documents", "Meetings", "/documents/meetings/"),
    ("documents", "Insurance", "/documents/insurance/"),
    ("documents", "Welcome packet", "/documents/welcome/"),
    ("documents", "Records requests", "/units/records/requests/"),
]

MODULE_SECTIONS = {
    ("requests", "maintenance"): "owner_requests",
    ("requests", "architectural"): "arc",
    ("requests", "violations"): "violations",
    ("requests", "preventive"): "work_orders",
    ("requests", "components"): "work_orders",
    ("community", "announcements"): "announcements",
    ("community", "calendar"): "calendar",
    ("community", "polls"): "polls",
    ("community", "directory"): "directory",
}


def _owners():
    return {
        (c.value.hub, c.value.subtab.slug): c.module for c in slots.contributions(slots.MENU_TABS)
    }


def test_module_sections_come_from_their_modules():
    owners = _owners()
    for key, module_id in MODULE_SECTIONS.items():
        assert owners[key] == module_id, key


def test_other_sections_are_core():
    owners = _owners()
    core = {key for key, module_id in owners.items() if module_id is None}
    assert core == set(owners) - set(MODULE_SECTIONS)
    assert ("governance", "votes") in core
    assert ("governance", "board") in core
    assert ("community", "messages") in core


def test_module_hooks_run_after_every_app_is_ready():
    assert list(apps.get_app_configs())[-1].name == "homeoa.plugins.hooks"


def test_board_menu_lists_every_section_in_order():
    menu = [
        (hub.id, str(tab.label), hub.subtab_url(i))
        for hub in nav.get_hubs()
        for i, tab in enumerate(hub.subtabs)
    ]
    assert menu == BOARD_MENU


def test_board_member_sees_community_sections_in_menu_order(board_client):  # noqa: F811
    client, usr, assoc = board_client
    request = RequestFactory().get("/")
    request.user, request.association = usr, assoc
    hidden = nav.hidden_subtab_urls(request)
    expected = [url for hub, _label, url in BOARD_MENU if hub == "community" and url not in hidden]
    html = client.get("/community/announcements/").content.decode()
    positions = [html.find(f'href="{url}"') for url in expected]
    assert len(expected) >= 3
    assert -1 not in positions
    assert positions == sorted(positions)
