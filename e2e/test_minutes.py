"""Attendance by unit for an owners meeting with a live htmx
quorum panel, a proxy that counts once and stops counting when revoked, the quorum
setting per meeting kind (rule-pack default first), then minutes drafted, approved at a
later meeting (immutable) and published as a PDF under Meetings, visible to owners.

The fixed E2E today is Oct 7, 2026, so the Nov 20 meeting is still open for revocation."""

import re

from playwright.sync_api import expect

from conftest import OWNER_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL
from helpers import m2

TITLE = "E2E Annual meeting minutes"
LATER = "E2E Board meeting minutes"
BODY = "Called to order at 7:00 pm. Units A and B present; Unit C by proxy."


def _schedule(page, kind, title, date):
    page.goto("/governance/meetings/")
    page.get_by_label("Kind").select_option(label=kind)
    page.get_by_label("Title (optional)").fill(title)
    page.get_by_label("Date").fill(date)
    page.get_by_role("button", name="Schedule meeting").click()
    expect(page.get_by_test_id("meeting-status")).to_have_text("Draft")
    return page.url


def _attend(page, unit, mode):
    sel = page.get_by_label(f"How Unit {unit} attended")
    with page.expect_response(re.compile(r"/attendance/set/$")):
        sel.select_option(label=mode)
    # Wait until htmx has processed the swapped-in panel before the next change.
    page.wait_for_function(
        "() => [...document.querySelectorAll('#attendance-panel form')]"
        ".every(f => f['htmx-internal-data'])"
    )


def _ctx(browser, base_url, login, email):
    ctx = browser.new_context(base_url=base_url)
    p = ctx.new_page()
    login(email, p)
    return ctx, p


def test_attendance_quorum_minutes(page, browser, base_url, seed, login, axe):
    m2.residents_enabled(True)  # Portal on for the owner check
    login(SECRETARY_EMAIL)
    meeting_url = _schedule(page, "Annual owners meeting", TITLE, "2026-11-20")
    _schedule(page, "Board meeting", LATER, "2026-12-01")

    # --- attendance and quorum (rule-pack default first) -----------------------------
    page.goto(meeting_url)
    page.get_by_test_id("attendance-link").click()
    status = page.get_by_test_id("quorum-status")
    panel = page.get_by_test_id("quorum")
    expect(status).to_have_text("Quorum not met")
    expect(panel).to_contain_text("0 of 9 present")
    expect(panel).to_contain_text("Needs 25% of all votes.")
    axe(page)

    # Bylaws setting: 3 units present.
    page.get_by_label("Basis").select_option(value="count")
    page.get_by_label("Value").fill("3")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("Quorum setting saved for this kind of meeting.")).to_be_visible()
    expect(panel).to_contain_text("Needs 3 present.")

    _attend(page, "A", "In person")
    expect(panel).to_contain_text("1 of 9 present")
    _attend(page, "B", "Remote")
    expect(panel).to_contain_text("2 of 9 present (1 in person, 1 remote, 0 by proxy)")
    expect(status).to_have_text("Quorum not met")

    # A proxy for a unit already present is ignored.
    page.get_by_label("Grantor unit").select_option(label="A")
    page.get_by_label("Proxy holder").fill("Board secretary")
    page.get_by_role("button", name="Record proxy").click()
    expect(page.get_by_text("Proxy recorded.")).to_be_visible()
    expect(panel).to_contain_text("2 of 9 present (1 in person, 1 remote, 0 by proxy)")

    # A proxy for an absent unit counts once: quorum met.
    page.get_by_label("Grantor unit").select_option(label="C")
    page.get_by_label("Proxy holder").fill("Owner of Unit A")
    page.get_by_role("button", name="Record proxy").click()
    expect(panel).to_contain_text("3 of 9 present (1 in person, 1 remote, 1 by proxy)")
    expect(status).to_have_text("Quorum met")
    axe(page)

    # Revoke C's proxy: quorum not met again.
    row = page.get_by_test_id("proxies").locator("tbody tr").filter(has_text="Owner of Unit A")
    row.get_by_role("button", name="Revoke").click()
    expect(page.get_by_text("Proxy revoked.")).to_be_visible()
    expect(row).to_contain_text("Revoked")
    expect(panel).to_contain_text("2 of 9 present")
    expect(status).to_have_text("Quorum not met")

    # Back to met by attendance (htmx, no reload).
    _attend(page, "D", "In person")
    expect(status).to_have_text("Quorum met")

    # --- minutes: draft -> approved (immutable) -> published --------------------------
    page.goto(meeting_url)
    page.get_by_test_id("minutes-link").click()
    expect(page.get_by_test_id("minutes-status")).to_have_text("Not started")
    page.get_by_test_id("minutes-body").fill(BODY)
    page.get_by_role("button", name="Save draft").click()
    expect(page.get_by_text("Draft minutes saved.")).to_be_visible()
    expect(page.get_by_test_id("minutes-status")).to_have_text("Draft")
    expect(page.get_by_test_id("minutes-body")).to_have_value(BODY)
    axe(page)

    approved_at = page.get_by_label("Approved at")
    later = approved_at.locator("option", has_text=LATER).get_attribute("value")
    approved_at.select_option(value=later)
    page.get_by_label("Approved on").fill("2026-12-01")  # not before the approving meeting
    page.get_by_role("button", name="Approve minutes").click()
    expect(page.get_by_text("Minutes approved. They can no longer be changed.")).to_be_visible()
    expect(page.get_by_test_id("minutes-status")).to_have_text("Approved")
    expect(page.get_by_test_id("minutes-body")).to_have_count(0)
    expect(page.get_by_test_id("minutes-text")).to_contain_text("Called to order")
    expect(page.get_by_text(re.compile(rf"Approved at {LATER} on "))).to_be_visible()
    # Immutable: a direct save is refused.
    minutes_url = page.url
    page.request.post(minutes_url + "save/", form={"body": "tampered"})
    page.reload()
    expect(page.get_by_test_id("minutes-text")).not_to_contain_text("tampered")

    page.get_by_label("Who can see it").select_option(value="owners")
    page.get_by_role("button", name="Publish as PDF").click()
    expect(page.get_by_text("Minutes published to the document library.")).to_be_visible()
    expect(page.get_by_test_id("minutes-status")).to_have_text("Published")
    expect(page.get_by_test_id("minutes-document")).to_be_visible()
    axe(page)

    page.goto("/documents/meetings/")
    link = page.get_by_role("link", name=re.compile(rf"Minutes: {TITLE}"))
    expect(link).to_be_visible()
    doc_url = link.get_attribute("href")

    ctx, owner = _ctx(browser, base_url, login, OWNER_EMAIL)
    owner.goto("/me/docs/")
    expect(owner.get_by_test_id("docs-list")).to_contain_text(f"Minutes: {TITLE}")
    ctx.close()
    ctx, tenant = _ctx(browser, base_url, login, TENANT_EMAIL)
    tenant.goto("/me/docs/")
    expect(tenant.get_by_text(f"Minutes: {TITLE}")).to_have_count(0)
    assert tenant.request.get(doc_url + "download/", max_redirects=0).status == 404
    ctx.close()
