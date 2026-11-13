"""COI/license + contracts: expiry to-do 30 days before expiry (idempotent job),
"Expired <date>" badge, auto-renew to-do on the cancel-by date, "Send request"
emails the primary contact once, owners refused. Today is 2026-10-07 (fixed clock)."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import m2

GREENWAY = "/vendors/greenway-landscaping/"
COI_TODO = "Request new insurance certificate: Greenway Landscaping"
RENEW_TODO = "Decide on renewal: Mowing 2026"


def _add_coi(page, expiry: str, number="COI-1"):
    page.goto(GREENWAY)
    page.get_by_role("link", name="Add certificate").click()
    page.locator("#id_kind").select_option("coi")
    page.locator("#id_issuer").fill("Cascade Mutual")
    page.locator("#id_number").fill(number)
    page.locator("#id_effective_date").fill("2025-09-01")
    page.locator("#id_expiry_date").fill(expiry)
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Certificate saved.")).to_be_visible()
    doc = page.locator("[data-testid^='vendor-doc-']", has_text=number)
    expect(doc).to_be_visible()
    return doc


def _add_contract(page, end: str, notice_days: str):
    page.goto(GREENWAY)
    page.get_by_role("link", name="Add contract").click()
    page.locator("#id_title").fill("Mowing 2026")
    page.locator("#id_start_date").fill("2026-01-01")
    page.locator("#id_end_date").fill(end)
    page.locator("#id_auto_renew").check()
    page.locator("#id_notice_days").fill(notice_days)
    page.locator("#id_amount").fill("6000.00")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Contract saved.")).to_be_visible()
    c = page.locator("[data-testid^='vendor-contract-']", has_text="Mowing 2026")
    expect(c).to_contain_text("Auto-renews")
    return c


def _todo_count(page, text) -> int:
    page.goto("/")
    page.wait_for_load_state()
    return page.get_by_text(text, exact=True).count()


def test_coi_todo_at_30_days(page, seed, login, axe):
    login(BOARD_EMAIL)
    doc = _add_coi(page, expiry="2026-12-15")  # window opens 2026-11-15
    expect(doc).to_contain_text("Current")
    axe(page)
    m2.run_scheduled("2026-11-14")
    assert _todo_count(page, COI_TODO) == 0
    m2.run_scheduled("2026-11-15")
    m2.run_scheduled("2026-11-16")  # idempotent
    assert _todo_count(page, COI_TODO) == 1


def test_contract_renewal_todo_on_cancel_by_date(page, seed, login, axe):
    login(BOARD_EMAIL)
    c = _add_contract(page, end="2026-12-31", notice_days="45")
    expect(c).to_contain_text("cancel by 2026-11-16")
    axe(page)
    m2.run_scheduled("2026-11-15")
    assert _todo_count(page, RENEW_TODO) == 0
    m2.run_scheduled("2026-11-16")
    m2.run_scheduled("2026-11-16")
    assert _todo_count(page, RENEW_TODO) == 1
    page.goto(GREENWAY)
    page.get_by_role("button", name="Mark renewal decided").click()
    expect(page.get_by_text("Marked done.")).to_be_visible()
    assert _todo_count(page, RENEW_TODO) == 0


def test_expired_badge_on_list(page, seed, login, axe):
    login(BOARD_EMAIL)
    doc = _add_coi(page, expiry="2026-09-30")  # expired before today
    expect(doc).to_contain_text("Expired")
    page.goto("/vendors/")
    expect(page.get_by_test_id("vendor-expired-greenway-landscaping")).to_contain_text(
        "Expired Sep 30, 2026"
    )
    expect(page.locator("[data-testid^='vendor-expired-']")).to_have_count(1)
    axe(page)


def test_send_request_emails_primary_contact_once(page, seed, login, mail):
    m2.residents_enabled(True)  # Real sends only when switched on
    login(BOARD_EMAIL)
    doc = _add_coi(page, expiry="2026-10-20")
    doc.get_by_role("button", name="Send request").click()
    expect(page.get_by_text(re.compile(r"Request sent to \S+@\S+\."))).to_be_visible()
    to = re.search(r"Request sent to (\S+)\.", page.get_by_text("Request sent to").inner_text())
    m2.drain_jobs()
    msg = mail.wait_for(to.group(1))
    sent = [m for m in mail.messages() if to.group(1).lower() in (a.lower() for a in m.to)]
    assert len(sent) == 1, [(m.to, m.subject) for m in sent]
    assert "certificate" in (msg.subject + msg.body).lower(), msg.subject
    doc = page.locator("[data-testid^='vendor-doc-']", has_text="COI-1")
    expect(doc).to_contain_text(re.compile(r"requested 2026-\d\d-\d\d from "))


def test_owner_refused(page, seed, login):
    login(OWNER_EMAIL)
    resp = page.goto(GREENWAY)
    assert resp and resp.status == 403, resp and resp.status
