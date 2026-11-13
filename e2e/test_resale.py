"""Resale certificate. The selling owner asks under More; the request is a to-do on Home
with the 10-day due date; the board sees HomeOA's pre-filled answers, answers the rest,
and the Treasurer issues it with the fee; the seller downloads the PDF."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from test_portal import billed, residents_on  # noqa: F401


def test_seller_asks_board_issues(page, residents_on, login, axe):  # noqa: F811
    login(OWNER_EMAIL)
    page.goto("/me/more/")
    page.get_by_test_id("more-resale").click()
    expect(page.get_by_role("heading", name="Resale certificate", level=1)).to_be_visible()
    axe(page)
    page.get_by_label("Send it to").fill("escrow@title.example")
    page.get_by_role("button", name="Send request").click()
    expect(page.get_by_text("Request sent. The board will send it by 2026-10-17.")).to_be_visible()
    page.context.clear_cookies()

    login(BOARD_EMAIL)
    page.goto("/")
    todo = page.locator('[id^="todo-resale-"]')
    expect(todo).to_contain_text("2026-10-17")
    todo.get_by_role("link").first.click()
    expect(page.get_by_test_id("resale-unverified")).to_have_count(0)  # statute text
    answers = page.get_by_test_id("resale-answers")
    expect(answers.locator("#a-b")).to_contain_text("Monthly assessment")
    axe(page)
    for box in answers.locator("textarea").all():
        if not box.input_value().strip():
            box.fill("None.")
    answers.get_by_role("button", name="Save answers").click()
    expect(page.get_by_text("Answers saved.")).to_be_visible()
    issue = page.get_by_test_id("resale-issue")
    issue.get_by_label("Fee charged to the unit owner").fill("275.00")
    issue.get_by_role("button", name="Issue and send").click()
    expect(page.get_by_text("Issued and emailed.")).to_be_visible()
    expect(page.get_by_test_id("resale-pdf")).to_be_visible()
    page.context.clear_cookies()

    login(OWNER_EMAIL)
    page.goto("/me/resale/")
    mine = page.get_by_test_id("my-resale-requests")
    expect(mine).to_contain_text("Issued")
    expect(mine.get_by_role("link", name="Download")).to_be_visible()


def test_board_sends_a_lender_the_unpaid_assessments_statement(page, login, axe):
    """A lender asks for a statement of unpaid assessments (RCW 64.34.364(15)): the board
    logs it on Units > Resale certificates, HomeOA fills in the unpaid assessments, and the
    statement is due in 15 days."""
    login(BOARD_EMAIL)
    page.goto("/units/resale/")
    form = page.get_by_test_id("resale-log-form")
    form.get_by_label("Statement of unpaid assessments").check()
    form.get_by_label("Unit").select_option(label="Unit C")
    form.get_by_label("Requested by (agent, escrow, lender or owner)").fill("Evergreen Bank")
    form.get_by_label("Send it to").fill("loans@bank.example")
    form.get_by_role("button", name="Log request").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text(
        "Statement of unpaid assessments: Unit C"
    )
    expect(page.get_by_test_id("resale-status")).to_contain_text("Due Oct 22, 2026")
    answers = page.get_by_test_id("resale-answers")
    expect(answers.locator("#a-1")).not_to_be_empty()
    axe(page)
    page.get_by_test_id("resale-issue").get_by_role("button", name="Issue and send").click()
    expect(page.get_by_text("Issued and emailed.")).to_be_visible()
    expect(page.get_by_test_id("resale-pdf")).to_be_visible()
