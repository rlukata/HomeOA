"""Non-binding owner polls. The board creates a poll on Community > Polls; an owner sees
it on Home, answers and changes the answer; the board sees one unit answered."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from test_portal import billed, residents_on  # noqa: F401


def test_board_asks_owner_answers(page, residents_on, login, axe):  # noqa: F811
    login(BOARD_EMAIL)
    page.goto("/community/polls/")
    form = page.get_by_test_id("poll-form")
    form.get_by_label("Question").fill("Repaint the doors blue?")
    form.get_by_label("Choices, one per line").fill("Yes\nNo")
    form.get_by_label("Opens on").fill("2026-10-07")
    form.get_by_label("Closes on").fill("2026-10-20")
    axe(page)
    form.get_by_role("button", name="Create poll").click()
    expect(page.get_by_text("Poll created.")).to_be_visible()
    poll_url = page.url
    page.context.clear_cookies()

    login(OWNER_EMAIL)
    page.goto("/me/")
    card = page.get_by_test_id("poll-card")
    expect(card).to_contain_text("Repaint the doors blue?")
    card.get_by_role("link", name="Answer").click()
    axe(page)
    page.get_by_role("radio", name="Yes").check()
    page.get_by_role("button", name="Send answer").click()
    expect(page.get_by_text("Thanks, your answer is saved.")).to_be_visible()
    page.get_by_role("radio", name="No").check()
    page.get_by_role("button", name="Change answer").click()
    page.context.clear_cookies()

    login(BOARD_EMAIL)
    page.goto(poll_url)
    results = page.get_by_test_id("poll-results")
    expect(results).to_contain_text("1 of 9 units answered")
    expect(results).to_contain_text("No: 1")
