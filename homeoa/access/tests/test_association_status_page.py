"""Settings > Association status: Setting up or Live, the go-live date, and the System
admin's emergency pause of resident access."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import services
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core import status
from homeoa.core.context import association_context
from homeoa.core.models import Association

User = get_user_model()

TODAY = dt.date(2026, 10, 15)
URL = "/settings/roles/status/"


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def admin(client, assoc):
    usr = User.objects.create_user("admin@example.org")
    grant(usr, "sys_admin", association=assoc)
    client.force_login(usr)
    return usr


def _fresh(assoc):
    return Association.objects.get(pk=assoc.pk)


def test_without_a_go_live_date_the_page_says_setting_up_and_waiting(client, assoc, admin):
    page = client.get(URL).content.decode()
    assert "Setting up" in page
    assert "The System admin goes live on the Go live page." in page
    assert "waiting for go-live" in page
    assert "Pause resident access" in page


def test_a_future_go_live_date_is_shown_as_goes_live_on(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 11, 1), reason="test")
    page = client.get(URL).content.decode()
    assert "Setting up" in page
    assert "Goes live on Nov 1, 2026." in page


def test_a_past_go_live_date_is_shown_as_live_since(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    page = client.get(URL).content.decode()
    assert "Live since Aug 1, 2026." in page
    assert "Resident access is open." in page


def test_pausing_needs_a_reason_and_confirmation(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    assert client.post(URL, {"reason": "Wrong balances"}).status_code == 200
    assert client.post(URL, {"reason": "  ", "confirm": "on"}).status_code == 200
    assert services.residents_enabled(_fresh(assoc))


def test_pausing_closes_resident_access_and_is_audited(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    resp = client.post(URL, {"reason": "Wrong balances", "confirm": "on"})
    assert resp.status_code == 302
    assoc = _fresh(assoc)
    assert not services.residents_enabled(assoc)
    row = status.status_for(assoc)
    assert row.paused_by == admin and row.pause_reason == "Wrong balances"
    event = AuditEvent.objects.filter(action="association.residents_enabled").latest("pk")
    assert event.changes["reason"] == "Wrong balances"
    page = client.get(URL).content.decode()
    assert "Resident access is paused." in page and "Wrong balances" in page
    assert "Lift the pause" in page


def test_lifting_the_pause_reopens_resident_access_when_live(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    status.pause_residents(assoc, reason="Emergency")
    resp = client.post(URL, {"reason": "Fixed", "confirm": "on"})
    assert resp.status_code == 302
    assert services.residents_enabled(_fresh(assoc))


def test_the_pause_can_be_set_before_go_live_and_explains_access_starts_at_go_live(
    client, assoc, admin
):
    assert "residents have no access now" in client.get(URL).content.decode()
    client.post(URL, {"reason": "Hold after go-live", "confirm": "on"})
    assert status.status_for(_fresh(assoc)).residents_paused
    assert "residents get access only from the go-live date" in client.get(URL).content.decode()


def test_board_member_without_settings_power_cannot_open_it_or_see_the_link(client, assoc):
    usr = User.objects.create_user("board@example.org")
    grant(usr, association=assoc)
    client.force_login(usr)
    assert client.get(URL).status_code == 403
    assert client.post(URL, {"reason": "x", "confirm": "on"}).status_code == 403
    assert not status.status_for(_fresh(assoc)).residents_paused
    assert URL.encode() not in client.get("/settings/").content


def test_settings_page_links_association_status_for_the_system_admin(client, assoc, admin):
    page = client.get("/settings/").content.decode()
    assert URL in page and "Association status" in page
