"""Hearings, decisions and fines on the owner ledger (VIO-04, VIO-05).
Server clock is fixed Oct 7, so past dates (cure deadline,
hearing day) are set by backdating rows via the manage shell."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m5
from helpers.seed import run_script
from test_violations import _category, _fill_log, _schedule


def _open(page, tmp_path, unit="I", furnished="2026-08-25"):
    _category(page)
    _schedule(page, furnished=furnished)
    _fill_log(page, m5.png(tmp_path, "bins.png"), unit)
    expect(page.get_by_text("Violation logged.")).to_be_visible()
    page.get_by_test_id("send-courtesy").click()
    page.get_by_test_id("send-formal").click()
    expect(page.locator("tr[data-kind=formal]")).to_be_visible()
    return page.url


def _schedule_hearing(page):
    form = page.get_by_test_id("hearing-form")
    form.get_by_label("Hearing date").fill("2026-10-21")
    form.get_by_label("Place or online link").fill("Clubhouse")
    form.get_by_test_id("schedule-hearing").click()
    expect(page.get_by_text("Hearing scheduled and notice sent.")).to_be_visible()


def _backdate():
    run_script(
        "import datetime as dt\n"
        "from homeoa.enforcement.models import ViolationStep, Hearing\n"
        "ViolationStep.all_associations.filter(kind='formal').update("
        "sent_on=dt.date(2026, 9, 1), cure_by=dt.date(2026, 9, 15))\n"
        "Hearing.all_associations.update(hearing_on=dt.date(2026, 10, 1))\n"
        "print('E2EJSON{}')\n",
        "backdate formal notice and hearing",
    )


def test_violation_to_fine(page, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    url = _open(page, tmp_path)
    _schedule_hearing(page)
    hearings = page.get_by_test_id("hearings")
    expect(hearings).to_contain_text("Oct 21, 2026")
    expect(page.get_by_test_id("decision-wait")).to_contain_text("Oct 21, 2026")
    expect(hearings.get_by_role("link", name="Notice PDF")).to_be_visible()
    axe(page)
    _backdate()
    page.goto(url)
    form = page.get_by_test_id("decision-form")
    expect(page.get_by_test_id("fine-blocked")).to_have_count(0)
    expect(form.get_by_label("Fine amount")).to_have_value("50.00")
    form.get_by_label("Yes").check()
    form.get_by_label("Owner statement").fill("Bins were moved the next day.")
    form.locator("select[name=outcome]").select_option("fine")
    form.get_by_test_id("record-decision").click()
    expect(page.get_by_text("Decision recorded and letter generated.")).to_be_visible()
    expect(page.get_by_test_id("outcome")).to_have_text("Fine")
    expect(page.get_by_test_id("fine")).to_contain_text("$50.00")
    expect(page.get_by_test_id("fine")).to_contain_text("No late fees or interest")
    expect(page.get_by_role("link", name="Decision letter PDF")).to_be_visible()
    axe(page)
    page.goto("/units/I/")
    expect(
        page.get_by_text(re.compile("Violation fine: Trash bins left out")).first
    ).to_be_visible()
    page.goto(url)
    page.get_by_label("Reason").fill("Board waived on appeal")
    page.get_by_test_id("void-fine").click()
    expect(page.get_by_text("Fine voided.")).to_be_visible()
    expect(page.get_by_test_id("fine-void")).to_be_visible()


def test_dismiss_posts_nothing(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    url = _open(page, tmp_path)
    _schedule_hearing(page)
    _backdate()
    page.goto(url)
    form = page.get_by_test_id("decision-form")
    form.locator("select[name=outcome]").select_option("dismissed")
    form.get_by_test_id("record-decision").click()
    expect(page.get_by_text("Decision recorded and letter generated.")).to_be_visible()
    expect(page.get_by_test_id("outcome")).to_have_text("Dismissed")
    expect(page.get_by_test_id("fine")).to_have_count(0)
    page.goto("/units/I/")
    expect(page.get_by_text(re.compile("Violation fine"))).to_have_count(0)


def test_hearing_required_before_fine(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _open(page, tmp_path)
    expect(page.get_by_test_id("decision-form")).to_have_count(0)
    expect(page.get_by_test_id("hearing-form")).to_be_visible()


def test_unfurnished_schedule_blocks_fine(page, seed, login, tmp_path):
    """Hard block when the schedule was not furnished by the violation date."""
    login(BOARD_EMAIL)
    url = _open(page, tmp_path, furnished=None)
    _schedule_hearing(page)
    _backdate()
    page.goto(url)
    expect(page.get_by_test_id("fine-blocked")).to_contain_text("no fine can be imposed")
    expect(page.get_by_test_id("decision-form").get_by_label("Fine amount")).to_have_count(0)


def _decide_fine(page, url):
    _backdate()
    page.goto(url)
    form = page.get_by_test_id("decision-form")
    form.locator("select[name=outcome]").select_option("fine")
    form.get_by_test_id("record-decision").click()
    expect(page.get_by_text("Decision recorded and letter generated.")).to_be_visible()
    expect(page.get_by_test_id("fine")).to_contain_text("$50.00")


def test_reschedule_then_fine(page, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    url = _open(page, tmp_path)
    _schedule_hearing(page)
    expect(page.locator("tr[data-kind=hearing_notice]")).to_have_count(1)
    page.get_by_test_id("hearing-change").locator("summary").click()
    form = page.get_by_test_id("reschedule-form")
    # Minimum-days check: a date too soon is refused server-side.
    date = form.locator("input[name=hearing_on]")
    date.evaluate("el => el.removeAttribute('min')")
    date.fill("2026-10-08")
    form.get_by_test_id("reschedule-hearing").click()
    expect(page.get_by_text(re.compile("The hearing must be at least"))).to_be_visible()
    expect(page.get_by_test_id("hearings")).to_contain_text("Oct 21, 2026")
    page.get_by_test_id("hearing-change").locator("summary").click()
    form.locator("input[name=hearing_on]").fill("2026-10-28")
    form.locator("input[name=place]").fill("Community room")
    form.get_by_test_id("reschedule-hearing").click()
    expect(page.get_by_text("Hearing rescheduled and updated notice sent.")).to_be_visible()
    expect(page.get_by_test_id("hearings")).to_contain_text("Oct 28, 2026")
    expect(page.get_by_test_id("hearings")).to_contain_text("Community room")
    expect(page.locator("tr[data-kind=hearing_notice]")).to_have_count(2)
    axe(page)
    _decide_fine(page, url)


def test_cancel_then_new_hearing(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _open(page, tmp_path)
    _schedule_hearing(page)
    page.get_by_test_id("hearing-change").locator("summary").click()
    page.get_by_test_id("cancel-form").get_by_label("Reason").fill("Owner asked to move")
    page.get_by_test_id("cancel-hearing").click()
    expect(page.get_by_text("Hearing cancelled.")).to_be_visible()
    expect(page.get_by_test_id("hearing-cancelled")).to_have_text("Cancelled")
    expect(page.get_by_test_id("decision-form")).to_have_count(0)
    _schedule_hearing(page)
    expect(page.get_by_test_id("hearing-cancelled")).to_have_count(1)
    expect(page.get_by_test_id("hearing-change")).to_be_visible()


def test_mediation_export_shows_fine(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    url = _open(page, tmp_path)
    _schedule_hearing(page)
    _decide_fine(page, url)
    page.goto("/units/I/file/")
    page.get_by_test_id("ledger-export").click()
    row = page.get_by_test_id("unit-file").locator('tbody tr[data-kind="ledger_exports"]')
    expect(row).to_have_count(1)
    csv = page.request.get(row.get_by_role("link", name="CSV").get_attribute("href"))
    assert csv.status == 200, csv.status
    fines = [ln for ln in csv.text().splitlines() if ",Fine," in ln or ln.endswith(",Fine")]
    assert fines and "50.00" in fines[0], csv.text()[:800]
