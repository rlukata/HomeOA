"""Per-user Home to-dos: an "Approve bill" item shows only on its approver's Home.
The entrant (not an approver of their own bill) sees none; each approver sees exactly one,
addressed to them; it leaves that approver's Home once they approve, and stays for the other."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL
from test_approvals import REPAIR, THIRD_EMAIL, _submitted, rules  # noqa: F401

APPROVE = re.compile(r"^Approve bill", re.I)


def _approve_todos(page):
    page.goto("/")
    expect(page.locator("#todo-card")).to_be_visible()
    return page.locator('[id^="todo-ap-approve-"]')


def _as(page, login, email):
    page.context.clear_cookies()
    login(email)


def test_approve_todo_only_for_its_approver(page, rules, login, axe):  # noqa: F811
    url = _submitted(page, login, "INV-F11", (REPAIR,))

    login(BOARD_EMAIL)  # entrant: not an approver of this bill
    expect(_approve_todos(page)).to_have_count(0)
    expect(page.locator("#todo-card").get_by_text(APPROVE)).to_have_count(0)

    _as(page, login, SECRETARY_EMAIL)
    mine = _approve_todos(page)
    expect(mine).to_have_count(1)
    expect(mine).to_contain_text("Approve bill")
    axe(page)
    page.goto(url)
    page.get_by_test_id("bill-approve").click()
    expect(page.get_by_test_id("approval-progress")).to_have_text("1 of 2 approvals")
    expect(_approve_todos(page)).to_have_count(0)  # done: gone from their Home

    _as(page, login, THIRD_EMAIL)
    theirs = _approve_todos(page)
    expect(theirs).to_have_count(1)
    expect(theirs).to_contain_text("1 of 2 approvals")
    page.goto(url)
    expect(page.get_by_test_id("bill-approve")).to_be_visible()
