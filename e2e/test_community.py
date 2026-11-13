"""The board posts one announcement to tenants and one to
owners -> the tenant sees only the tenants post; a calendar event shows in the tenant's
iCal feed; a revoked iCal link returns 404."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, TENANT_EMAIL
from helpers import m2

TENANT_POST = "E2E Water shutoff (tenants)"
OWNER_POST = "E2E Special assessment vote (owners)"
EVENT = "E2E Garage sweeping"


def _announce(page, title, audience):
    page.goto("/community/announcements/new/")
    page.get_by_label("Title").fill(title)
    page.get_by_label("Message").fill(f"{title} body")
    page.get_by_label("Audience").select_option(label=audience)
    page.get_by_role("button", name="Save", exact=True).click()
    page.goto("/community/announcements/")
    expect(page.get_by_test_id("announcement-list")).to_contain_text(title)


def test_announcement_audience(page, browser, base_url, seed, login, axe):
    m2.residents_enabled(True)
    login(BOARD_EMAIL)
    _announce(page, TENANT_POST, "Tenants")
    _announce(page, OWNER_POST, "Owners")
    axe(page)

    page.goto("/community/calendar/new/")
    page.get_by_label("Title").fill(EVENT)
    page.get_by_label("Start date").fill("2026-10-20")
    page.get_by_label("Audience").select_option(label="Everyone")
    page.get_by_role("button", name="Save", exact=True).click()
    page.goto("/community/calendar/")
    expect(page.get_by_text(EVENT)).to_be_visible()
    axe(page)

    ctx = browser.new_context(base_url=base_url)
    tenant = ctx.new_page()
    login(TENANT_EMAIL, tenant)
    tenant.goto("/community/me/")
    ann = tenant.get_by_test_id("announcements")
    expect(ann).to_contain_text(TENANT_POST)
    expect(ann).not_to_contain_text(OWNER_POST)
    expect(tenant.get_by_test_id("calendar")).to_contain_text(EVENT)
    axe(tenant)

    tenant.get_by_test_id("ical-create").click()
    feed_url = tenant.get_by_test_id("ical-url").input_value()
    anon = browser.new_context()
    r = anon.request.get(feed_url)
    assert r.ok, r.status
    body = r.text()
    assert "BEGIN:VCALENDAR" in body and EVENT in body, body[:500]
    assert OWNER_POST not in body

    tenant.get_by_test_id("ical-revoke").click()
    expect(tenant.get_by_test_id("ical-create")).to_be_visible()
    assert anon.request.get(feed_url).status == 404
    anon.close()
    ctx.close()
