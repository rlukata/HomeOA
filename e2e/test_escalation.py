"""Escalation ladder (VIO-03). Courtesy -> formal notice (cure_by = today + 14,
generic pack), email when every owner has e-notice consent (unit A in seed) else a
letter in a draft mail batch (unit C), Mark cured, and the "Cure deadline passed:
schedule hearing" to-do (step backdated via manage shell; server clock is fixed Oct 7)."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2, m5
from helpers.seed import run_script
from test_violations import _category, _fill_log, _schedule

PASSED = "Cure deadline passed: schedule hearing"


def _open(page, tmp_path, unit):
    _category(page)
    _schedule(page, furnished="2026-08-25")
    _fill_log(page, m5.png(tmp_path, "bins.png"), unit)
    expect(page.get_by_text("Violation logged.")).to_be_visible()
    return page.url


def _rows(page):
    return page.get_by_test_id("steps").locator("tbody tr")


def test_ladder_by_email_then_cured(page, seed, login, axe, tmp_path, mail):
    m2.residents_enabled(True)  # Owner email stays a preview while residents are off
    login(BOARD_EMAIL)
    _open(page, tmp_path, "A")
    # Formal requires courtesy first (generic pack).
    expect(page.get_by_test_id("send-formal")).to_be_disabled()
    page.get_by_test_id("send-courtesy").click()
    expect(page.get_by_text("Notice sent by email.")).to_be_visible()
    page.get_by_test_id("send-formal").click()
    expect(page.get_by_text("Notice sent by email.")).to_be_visible()
    rows = _rows(page)
    expect(rows).to_have_count(2)
    formal = page.locator("tr[data-kind=formal]")
    expect(formal).to_contain_text("Oct 21, 2026")
    expect(formal).to_contain_text("Email")
    m2.drain_jobs()
    msg = mail.wait_for(m2.OWNERA_EMAIL)
    assert any(ct == "application/pdf" for _n, ct in msg.attachments)
    axe(page)
    page.get_by_test_id("mark-cured").click()
    expect(page.get_by_text("Marked cured.")).to_be_visible()
    expect(page.get_by_test_id("status")).to_have_text("Cured")
    expect(page.get_by_text("Cured on Oct 7, 2026")).to_be_visible()


def test_no_consent_goes_to_mail_batch(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _open(page, tmp_path, "C")
    page.get_by_test_id("send-courtesy").click()
    expect(page.get_by_text("Letter generated and added to a draft mail batch")).to_be_visible()
    row = page.locator("tr[data-kind=courtesy]")
    expect(row).to_contain_text("Letter")
    expect(row.get_by_role("link", name="mail batch")).to_be_visible()
    expect(row.get_by_role("link", name="PDF")).to_be_visible()


def test_cure_passed_todo(page, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    url = _open(page, tmp_path, "A")
    page.get_by_test_id("send-courtesy").click()
    page.get_by_test_id("send-formal").click()
    expect(page.locator("tr[data-kind=formal]")).to_be_visible()
    expect(page.get_by_test_id("cure-passed")).to_have_count(0)
    run_script(
        "import datetime as dt, json\n"
        "from homeoa.enforcement.models import ViolationStep\n"
        "n = ViolationStep.all_associations.filter(kind='formal').update("
        "sent_on=dt.date(2026, 9, 1), cure_by=dt.date(2026, 9, 15))\n"
        "print('E2EJSON' + json.dumps({'n': n}))\n",
        "backdate formal notice",
    )
    page.goto(url)
    expect(page.get_by_test_id("cure-passed")).to_have_text(PASSED)
    page.goto("/")
    expect(page.get_by_text(PASSED)).to_be_visible()
    axe(page)
