"""The owner portal's module parts come from the OWNER_PORTAL slot, in a fixed order."""

import datetime as dt
import re
from types import SimpleNamespace

from homeoa import providers
from homeoa.community import polls
from homeoa.community.models import Announcement
from homeoa.enforcement.models import ViolationStep
from homeoa.plugins import slots
from homeoa.portal.contributions import PortalPart, RequestKind
from homeoa.portal.tests import test_requests_tab
from homeoa.portal.tests.test_requests_tab import OWNER_A, TENANT_E, _login, _on

# The Requests tab's synthetic association and one request of each kind.
pilot = test_requests_tab.pilot
data = test_requests_tab.data

EXPECTED = {
    ("request", "maintenance"): "owner_requests",
    ("request", "arc"): "arc",
    ("requests", "enforcement/_portal_requests.html"): "violations",
    ("home", "community/_portal_home_polls.html"): "polls",
    ("home", "community/_portal_home_announcements.html"): "announcements",
    ("more", "registry/_portal_more_link.html"): "directory",
    ("welcome_how", "registry/_portal_welcome_link.html"): "directory",
    ("pay_balance", "payments/_portal_autopay_line.html"): "online_payments",
    ("pay_methods", "payments/_portal_autopay_method.html"): "online_payments",
}


def _key(value):
    if isinstance(value, RequestKind):
        return ("request", value.kind)
    assert isinstance(value, PortalPart)
    return (value.place, value.template)


def test_each_module_portal_contribution_is_tagged_with_its_module():
    found = {_key(c.value): c.module for c in slots.contributions(slots.OWNER_PORTAL)}
    assert found == EXPECTED


def _testids(html, prefix):
    return re.findall(rf'data-testid="({prefix}[a-z-]*)"', html)


def test_requests_tab_lists_buttons_violations_and_requests_in_order(client, pilot, data):
    _on(pilot)
    ViolationStep.objects.create(
        violation=data["vio"], kind="courtesy", sent_on=dt.date(2026, 8, 20), delivery="email"
    )
    _login(client, OWNER_A)
    html = client.get("/me/requests/").content.decode()
    assert _testids(html, "req-new-") == ["req-new-maint", "req-new-arc"]
    assert re.findall(r'data-kind="([a-z]+)"', html) == ["violation", "maintenance", "arc"]
    assert re.findall(r"<b>([^<]+)</b>", html) == ["Noise", "Leaky gutter", "New front door"]


def test_tenant_requests_tab_has_only_the_maintenance_button(client, pilot, data):
    _on(pilot)
    _login(client, TENANT_E)
    html = client.get("/me/requests/").content.decode()
    assert _testids(html, "req-new-") == ["req-new-maint"]
    assert "violations-list" not in html


def test_more_page_puts_the_directory_link_before_owner_votes(client, pilot, data):
    _on(pilot)
    _login(client, OWNER_A)
    html = client.get("/me/more/").content.decode()
    assert _testids(html, "more-") == [
        "more-links",
        "more-welcome",
        "more-contacts",
        "more-resale",
        "more-records",
        "more-directory",
        "more-votes",
    ]
    _login(client, TENANT_E)
    assert "more-directory" not in client.get("/me/more/").content.decode()


def test_welcome_packet_links_to_the_resident_directory_last(client, pilot, data):
    _on(pilot)
    _login(client, OWNER_A)
    html = client.get("/me/welcome/").content.decode()
    how = html[html.index('data-testid="welcome-how"') :]
    how = how[: how.index("</section>")]
    assert re.findall(r'<a class="li" href="([^"?#]+)', how)[-1] == "/me/directory/"


def test_home_shows_polls_before_announcements(client, pilot, data):
    _on(pilot)
    Announcement.objects.create(
        association=pilot, title="Pool opens", body="x", publish_on=dt.date(2026, 10, 1)
    )
    polls.create(
        pilot,
        question="Paint color?",
        details="",
        choices=["Red", "Blue"],
        opens_on=dt.date(2026, 10, 1),
        closes_on=dt.date(2026, 10, 30),
        include_tenants=True,
        show_results=True,
    )
    _login(client, OWNER_A)
    html = client.get("/me/").content.decode()
    board = html.index("From the board")
    assert board < html.index("Paint color?") < html.index("Pool opens")
    assert html.index("Pool opens") < html.index("Building rules")


def test_pay_page_shows_the_autopay_line_and_link_when_online(client, pilot, data, monkeypatch):
    _on(pilot)
    method = SimpleNamespace(ref="m1", label="Checking 1234", chargeable=True)
    online = SimpleNamespace(
        online=True,
        pending=lambda unit, period: [],
        saved_methods=lambda unit, person: [method],
        setup_url=lambda unit: "/setup/",
        fee_quote=lambda association, amount: 0,
    )
    real = providers.get_provider
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda kind, a: online if kind == "PaymentProvider" else real(kind, a),
    )
    _login(client, OWNER_A)
    html = client.get("/me/pay/").content.decode()
    assert html.index('data-testid="autopay-line"') < html.index('data-testid="bank-accounts"')
    assert "Turn on autopay" in html
    accounts = html[html.index('data-testid="bank-accounts"') :]
    assert re.findall(r'<a class="li" href="([^"]+)"', accounts)[:2] == [
        "/setup/",
        "/me/pay/autopay/?unit=A",
    ]
