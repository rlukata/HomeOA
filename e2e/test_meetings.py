"""The board schedules the annual meeting
(Nov 20; Dec 3 is past the 50-day max
from the fixed E2E today of Oct 7)
-> Home shows "Send notice by" -> sending to all owners splits email vs letter by
e-notice consent -> every unit gets a comm log entry.

Demo seed: owners of A and G have e-notice consent; the other units get letters.
Postal provider is the fake (no Lob)."""

import re

from playwright.sync_api import expect

from conftest import SECRETARY_EMAIL
from helpers import m2
from helpers import seed as seed_helpers

UNITS = list("ABCDEFGHI")
EMAIL_UNITS = {"A", "G"}
TITLE = "E2E Annual meeting 2026"


def test_annual_meeting_notice(page, seed, login, axe):
    seed_helpers.billing()
    m2.residents_enabled(True)
    login(SECRETARY_EMAIL)

    page.goto("/governance/meetings/")
    page.get_by_label("Kind").select_option(label="Annual owners meeting")
    page.get_by_label("Title (optional)").fill(TITLE)
    page.get_by_label("Date").fill("2026-11-20")
    page.get_by_role("button", name="Schedule meeting").click()
    expect(page.get_by_test_id("meeting-status")).to_have_text("Draft")
    window = page.get_by_test_id("notice-window")
    expect(window).to_contain_text("Send notice")
    expect(window).to_contain_text("Send notice between Oct 1, 2026 and Nov 6, 2026.")
    expect(window).to_contain_text("RCW 64.90.445")
    axe(page)
    meeting_url = page.url

    page.goto("/")
    todo = page.get_by_text(re.compile(rf"Send notice by \w+ \d+: {TITLE}"))
    expect(todo.first).to_be_visible()

    # Owner notices are off before cutover: switch them on.
    page.goto("/governance/meetings/")
    page.get_by_role("button", name="Switch owner notices on").click()
    expect(page.get_by_test_id("owner-notices-live")).to_be_visible()

    page.goto(meeting_url)
    page.get_by_role("button", name="Send to all owners").click()
    expect(page.get_by_text(re.compile(r"Notice sent: 2 by email, 7 by mail"))).to_be_visible()
    expect(page.get_by_test_id("meeting-status")).to_have_text("Noticed")
    table = page.get_by_test_id("meeting-notices")
    for unit in UNITS:
        row = table.locator("tbody tr").filter(has_text=re.compile(rf"^\s*Unit {unit}\b"))
        how = "Email" if unit in EMAIL_UNITS else "Letter by mail"
        expect(row).to_have_count(1)
        expect(row.locator("td").nth(1)).to_contain_text(how)
    axe(page)

    page.goto("/")
    expect(page.get_by_text(re.compile(rf"Send notice by \w+ \d+: {TITLE}"))).to_have_count(0)

    m2.drain_jobs()
    for unit in UNITS:
        page.goto(f"/units/{unit}/comms/")
        log = page.get_by_test_id("comm-log")
        rows = log.locator("tbody tr").filter(has_text=re.compile("meeting", re.I))
        expect(rows).not_to_have_count(0)
    axe(page)


EMERGENCY_TITLE = "E2E Emergency board meeting"
EMERGENCY_REASON = "Burst water main flooding the garage"


def test_emergency_board_meeting(page, seed, login, axe):
    """An emergency board meeting uses the emergency_min_days pack key (the pilot's
    WA pack resolves it to 7 days; the generic placeholder is 1), goes out by email
    only (no letters, no print batch); non-consenting units are counted as skipped."""
    seed_helpers.billing()
    m2.residents_enabled(True)
    login(SECRETARY_EMAIL)

    page.goto("/governance/meetings/")
    page.get_by_label("Kind").select_option(label="Board meeting")
    page.get_by_label("Title (optional)").fill(EMERGENCY_TITLE)
    # Oct 20 from the fixed today of Oct 7: too late for WA's 14 days, fine for 7.
    page.get_by_label("Date").fill("2026-10-20")
    page.get_by_role("button", name="Schedule meeting").click()
    expect(page.get_by_test_id("meeting-emergency")).to_have_count(0)
    expect(page.get_by_test_id("notice-window")).to_contain_text("At least 14 days")
    meeting_url = page.url

    form = page.get_by_test_id("emergency-form")
    form.get_by_label("Emergency meeting for an unforeseeable event").check()
    form.get_by_label("Reason (required for an emergency meeting)").fill(EMERGENCY_REASON)
    form.get_by_role("button", name="Save emergency setting").click()

    expect(page.get_by_test_id("meeting-emergency")).to_have_text("Emergency")
    expect(page.get_by_text(f"Unforeseeable event: {EMERGENCY_REASON}")).to_be_visible()
    window = page.get_by_test_id("notice-window")
    expect(window).to_contain_text("At least 7 days before the meeting.")
    expect(window).to_contain_text("Send notice by Oct 13, 2026.")
    expect(window.get_by_text("Verified").first).to_be_visible()  # RCW 64.90.445(2)(f)
    note = page.get_by_test_id("emergency-note")
    expect(note).to_contain_text("sent by email only")
    expect(page.get_by_role("button", name="Print-only batch")).to_be_disabled()
    expect(page.get_by_text("Emergency meeting notice goes out by email only")).to_be_visible()
    axe(page)

    page.goto("/governance/meetings/")
    page.get_by_role("button", name="Switch owner notices on").click()
    expect(page.get_by_test_id("owner-notices-live")).to_be_visible()

    page.goto(meeting_url)
    page.get_by_role("button", name="Send to all owners").click()
    # WA (RCW 64.90.445(2)(f)): every owner with a known email gets it, consent or
    # not; units with no email on file are counted apart.
    expect(
        page.get_by_text(re.compile(r"Notice sent: 7 by email, 0 by mail, 0 to print"))
    ).to_be_visible()
    expect(page.get_by_text(re.compile(r"2 units have no email on file"))).to_be_visible()
    expect(page.get_by_test_id("meeting-status")).to_have_text("Noticed")
    table = page.get_by_test_id("meeting-notices")
    rows = table.locator("tbody tr")
    expect(rows).to_have_count(7)
    for unit in EMAIL_UNITS:
        row = rows.filter(has_text=re.compile(rf"^\s*Unit {unit}\b"))
        expect(row.locator("td").nth(1)).to_contain_text("Email")
    expect(table.get_by_role("link", name="mail batch")).to_have_count(0)
    axe(page)
