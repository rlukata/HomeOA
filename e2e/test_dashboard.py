"""Board Home cards (cash by fund, budget vs actual YTD,
AP due in 14 days, AR past due, compliance) each linking to its page; the CPA sees the
finance cards only (no compliance, no to-dos, no activity); the "Close <Month YYYY>"
to-do shows failing checks, turns "Ready", and moves on/clears after closing; it is
hidden without finance.manage. Clock fixed at 2026-10-07; seed_demo posts Aug-Sep 2026."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL
from helpers.seed import run_script
from test_cpa import CPA_EMAIL, _invite_cpa
from test_period_close import DRAFT_BILL, _fix

CARDS = {
    "dash-cash": re.compile(r"/banking/|/money/bank"),
    "dash-budget": re.compile(r"/budget"),
    "dash-ap": re.compile(r"/bills|/ap/|/payables"),
    "dash-ar": re.compile(r"aging"),
    "dash-compliance": re.compile(r"/compliance"),
}
# Dated before any ledger activity so it counts against the oldest open month.
EARLY_DRAFT = DRAFT_BILL.replace("dt.date(2026, 9, 20)", "dt.date(2025, 1, 2)")
CLOSE_TODO = 'li[id^="todo-close-"]'
TITLE = re.compile(r"^Close ([A-Z][a-z]+ \d{4})$")


def _close_todo(page):
    page.goto("/")
    expect(page.locator("#todo-card")).to_be_visible()
    return page.locator(CLOSE_TODO)


def test_treasurer_cards_link_to_pages(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/")
    for code in ("operating", "reserve", "insurance"):
        expect(page.get_by_test_id(f"dash-fund-{code}")).to_have_text(re.compile(r"\$[\d,]+\.\d\d"))
    expect(page.get_by_test_id("dash-expense-actual")).to_contain_text("of $")
    expect(page.get_by_test_id("dash-ap-total")).to_have_text(re.compile(r"\$[\d,]+\.\d\d"))
    expect(page.get_by_test_id("dash-ap")).to_contain_text("Bills due in 14 days")
    expect(page.get_by_test_id("dash-ar-total")).to_have_text(re.compile(r"\$[\d,]+\.\d\d"))
    expect(page.get_by_test_id("dash-compliance-open")).to_contain_text("open")
    axe(page)
    for testid, url in CARDS.items():
        page.goto("/")
        link = page.get_by_test_id(testid).get_by_role("link", name="Open")
        with page.expect_navigation() as nav:
            link.click()
        assert nav.value and nav.value.status == 200, (testid, nav.value and nav.value.status)
        expect(page).to_have_url(url)


def test_cpa_sees_finance_cards_only(page, seed, login, axe):
    login(BOARD_EMAIL)
    _invite_cpa(page)
    page.context.clear_cookies()
    login(CPA_EMAIL)
    page.goto("/")
    for testid in ("dash-cash", "dash-budget", "dash-ap", "dash-ar"):
        expect(page.get_by_test_id(testid)).to_be_visible()
    expect(page.get_by_test_id("dash-compliance")).to_have_count(0)
    expect(page.locator("#todo-card")).to_have_count(0)
    expect(page.locator("#activity-h")).to_have_count(0)
    expect(page.get_by_test_id("whats-next")).to_have_count(0)
    axe(page)


def test_whats_next_card_marks_the_next_setup_step_and_opens_it(page, seed, login, axe):
    # The demo has a board but never adopted the Features section.
    login(BOARD_EMAIL)
    page.goto("/")
    card = page.get_by_test_id("whats-next")
    expect(card.locator("li").first).to_contain_text("Board added")
    expect(card.locator("li").first).to_contain_text("Done")
    step = card.locator('li[aria-current="step"]')
    expect(step).to_contain_text("Features picked")
    expect(step).to_contain_text("Next")
    # The card lists the setup sections, so the To do card does not repeat them.
    expect(page.locator("#todo-card")).not_to_contain_text("Finish association setup")
    axe(page)
    step.get_by_role("link", name="Open").click()
    expect(page.locator("h1")).to_have_text("Features")


def test_close_todo_ready_then_moves_and_clears(page, seed, login, axe):
    run_script(EARLY_DRAFT, "draft bill")
    login(BOARD_EMAIL)
    todo = _close_todo(page)
    expect(todo).to_have_count(1)
    title = todo.locator(".att-t")
    expect(title).to_have_text(TITLE)
    first_month = TITLE.match(title.inner_text()).group(1)
    expect(todo.locator(".att-s")).to_contain_text("No draft bills")
    expect(todo.locator(".att-s")).not_to_contain_text("Ready")
    axe(page)

    _fix()  # removes the draft bill and reconciles every account through Sep 30
    todo = _close_todo(page)
    expect(todo.locator(".att-t")).to_have_text(f"Close {first_month}")
    expect(todo.locator(".att-s")).to_have_text(re.compile(r"^Ready"))

    seen = []
    for _ in range(12):
        todo = _close_todo(page)
        if todo.count() == 0:
            break
        month = TITLE.match(todo.locator(".att-t").inner_text()).group(1)
        assert month not in seen, f"to-do did not move on after closing {month}"
        seen.append(month)
        expect(todo.locator(".att-s")).to_have_text(re.compile(r"^Ready"))
        todo.get_by_role("link", name="Review").click()
        expect(page).to_have_url(re.compile(r"/money/close/\?month=\d{4}-\d{2}"))
        page.get_by_test_id("close-month").click()
        expect(page.get_by_test_id("locked-through")).to_contain_text("locked through")
    assert seen and seen[0] == first_month and seen[-1] == "September 2026", seen
    expect(_close_todo(page)).to_have_count(0)  # October 2026 has not ended


def test_close_todo_hidden_without_finance_manage(page, seed, login):
    run_script(EARLY_DRAFT, "draft bill")
    login(BOARD_EMAIL)
    expect(_close_todo(page)).to_have_count(1)
    page.context.clear_cookies()
    login(SECRETARY_EMAIL)
    expect(_close_todo(page)).to_have_count(0)
    expect(page.locator("#todo-card").get_by_text(re.compile(r"^Close \w+ \d{4}"))).to_have_count(0)
