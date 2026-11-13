"""Budget: draft, lines, adopt, budget vs actual, dues from budget, access.

Dues from a budget need the owners' ratification and a board resolution: the treasurer
submits the dues form citing the secretary's draft resolution, and the dues take effect
when the secretary adopts it."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL
from helpers import m2
from helpers import seed as seed_helpers

LINES = [
    ("Income", "Assessments", "4000 Assessment income", "48000.00"),
    ("Expense", "Landscaping", "5100 Landscaping", "6000.00"),
]


_CHARGE_COUNTS = """
import json
from collections import defaultdict
from homeoa.billing.models import Charge
out = defaultdict(lambda: defaultdict(int))
for c in Charge.all_associations.filter(kind="assessment", period__isnull=False):
    out[c.period.strftime("%Y-%m")][c.unit.number] += 1
print("E2EJSON" + json.dumps(out))
"""


# The ratification vote itself is covered by test_annual_meeting.
_RATIFY = """
import datetime as dt, json
from homeoa.budget.models import Budget, BudgetStatus
b = Budget.all_associations.filter(year=2026, status=BudgetStatus.ADOPTED).order_by("-pk").first()
b.ratification = "ratified"
b.ratification_on = dt.date(2026, 10, 1)
b.save()
print("E2EJSON" + json.dumps({"pk": b.pk}))
"""

RESOLUTION = "Set 2026 dues from the budget"


@pytest.fixture
def billed(seed):
    return seed_helpers.billing()  # October 2026 dues posted under the seeded schedule


def _create_draft(page, name="FY2026 draft"):
    page.goto("/money/budget/")
    page.get_by_role("link", name="New budget").click()
    page.locator("#id_year").fill("2026")
    page.locator("#id_name").fill(name)
    page.get_by_role("button", name="Create draft").click()
    expect(page.get_by_text("Draft budget created.")).to_be_visible()
    expect(page.get_by_role("heading", name=name, level=1)).to_be_visible()
    if page.url.endswith("/lines/"):  # a new draft opens on its lines; go to the budget
        page.goto(page.url.removesuffix("lines/"))


def _fill_lines(page, lines):
    page.get_by_role("link", name="Edit lines").click()
    for i, (group, label, account, amount) in enumerate(lines):
        row = f"[name$='-{i}-"
        page.locator(f"{row}group']").fill(group)
        page.locator(f"{row}label']").fill(label)
        page.locator(f"{row}account']").select_option(label=account)
        page.locator(f"{row}fund']").select_option(label="Operating")
        page.locator(f"{row}amount']").fill(amount)
    page.get_by_role("button", name="Save lines").click()
    expect(page.get_by_text("Budget lines saved.")).to_be_visible()
    if page.url.endswith("/lines/"):
        page.goto(page.url.removesuffix("lines/"))


def _adopted(page):
    _create_draft(page)
    _fill_lines(page, LINES)
    page.get_by_test_id("budget-adopt").click()
    expect(page.get_by_text("Budget adopted.")).to_be_visible()


def test_draft_edit_adopt(page, billed, login, axe):
    login(BOARD_EMAIL)
    _create_draft(page)
    axe(page)
    page.get_by_test_id("budget-adopt").click()
    expect(page.get_by_text("Add budget lines before adopting.")).to_be_visible()
    _fill_lines(page, LINES)
    expect(page.get_by_test_id("budget-net")).to_have_text("$42,000.00")
    # Edit a line again while still a draft.
    _fill_lines(page, [LINES[0], ("Expense", "Landscaping", "5100 Landscaping", "7000.00")])
    expect(page.get_by_test_id("budget-net")).to_have_text("$41,000.00")
    page.get_by_test_id("budget-adopt").click()
    expect(page.get_by_text("Budget adopted.")).to_be_visible()
    expect(page.get_by_role("link", name="Edit lines")).to_have_count(0)
    page.goto("/money/budget/")
    expect(page.get_by_test_id("budget-compare")).to_contain_text("Assessments")
    axe(page)


def test_budget_vs_actual(page, billed, login, axe):
    login(BOARD_EMAIL)
    _adopted(page)
    page.get_by_role("link", name="Budget vs actual").click()
    expect(page.get_by_test_id("bva-budget")).to_have_text("$6,000.00")
    expect(page.get_by_test_id("bva-elapsed")).to_have_text(re.compile(r"^\d+(\.\d+)?%$"))
    row = page.get_by_test_id("bva-row-4000-operating")
    expect(row).to_contain_text("$48,000.00")
    # October dues are posted, so assessment actuals are non-zero.
    expect(row.locator("td").nth(3)).not_to_have_text("$0.00")
    axe(page)


def _secretary(browser, base_url, login):
    ctx = browser.new_context(base_url=base_url)
    p = ctx.new_page()
    login(SECRETARY_EMAIL, p)
    return ctx, p


def test_dues_from_budget_replace(page, browser, base_url, billed, login, axe):
    login(BOARD_EMAIL)
    _adopted(page)
    page.get_by_test_id("budget-dues").click()
    dues_url = page.url
    expect(page.get_by_test_id("dues-annual")).to_have_text("$48,000.00")
    expect(page.get_by_test_id("dues-monthly-total")).to_have_text(
        re.compile(r"\$(3,999|4,000)\.\d\d")
    )
    expect(page.get_by_test_id("dues-unit-C")).to_be_visible()
    expect(page.get_by_text("Owners have not ratified this budget yet.")).to_be_visible()
    axe(page)
    seed_helpers.run_script(_RATIFY, "ratify budget")

    # The secretary drafts the resolution that will approve the dues.
    ctx, sec = _secretary(browser, base_url, login)
    sec.goto("/governance/board/")
    sec.locator("form[action$='/resolutions/add/']").get_by_label("Title").fill(RESOLUTION)
    sec.get_by_role("button", name="New draft resolution").click()

    page.goto(dues_url)
    expect(page.get_by_text("Owners have not ratified this budget yet.")).to_have_count(0)
    pick = page.locator("#id_change_resolution")
    pick.select_option(pick.locator("option", has_text=RESOLUTION).get_attribute("value"))
    # The seeded schedule covers 2026: creating without confirming is refused.
    page.get_by_test_id("dues-create").click()
    expect(page.get_by_text("A dues schedule already covers this year.")).to_be_visible()
    pick = page.locator("#id_change_resolution")
    pick.select_option(pick.locator("option", has_text=RESOLUTION).get_attribute("value"))
    page.get_by_test_id("dues-replace").check()
    page.get_by_test_id("dues-create").click()
    pending = page.get_by_text(re.compile(r"It takes effect when resolution 2026-\d+"))
    expect(pending).to_be_visible()
    expect(page.get_by_test_id("change-status")).to_have_text("Waiting for approval")
    change_url = page.url

    # Adopting the resolution applies the dues change.
    sec.goto("/governance/board/")
    row = sec.get_by_test_id("resolutions").locator("li, tr").filter(has_text=RESOLUTION)
    row.get_by_role("link").first.click()
    sec.get_by_role("button", name="Adopt resolution").click()
    ctx.close()
    page.goto(change_url)
    expect(page.get_by_test_id("change-status")).to_have_text("Approved and applied")

    # Mid-year replace (Aug-Oct already billed): each unit is still charged once a month.
    for day in ["2026-11-01", "2026-11-02", "2026-12-01"]:
        m2.run_scheduled(day)
    counts = seed_helpers.run_script(_CHARGE_COUNTS, "charge counts")
    assert set(counts) == {"2026-08", "2026-09", "2026-10", "2026-11", "2026-12"}, counts
    for month, per_unit in counts.items():
        assert sorted(per_unit) == list("ABCDEFGHI"), (month, per_unit)
        assert set(per_unit.values()) == {1}, (month, per_unit)


def test_non_treasurer_cannot_adopt(page, billed, login):
    login(BOARD_EMAIL)
    _create_draft(page)
    _fill_lines(page, LINES)
    url = page.url
    page.context.clear_cookies()
    login(SECRETARY_EMAIL)
    page.goto(url)
    expect(page.get_by_test_id("budget-adopt")).to_have_count(0)
    expect(page.get_by_role("link", name="Edit lines")).to_have_count(0)
    r = page.request.post(url + "adopt/")
    assert r.status == 403, r.status
    page.goto(url)
    expect(page.get_by_text("Draft", exact=True)).to_be_visible()


def test_owner_refused(page, billed, login):
    login(OWNER_EMAIL)
    resp = page.goto("/money/budget/")
    assert resp and resp.status == 403, resp and resp.status
