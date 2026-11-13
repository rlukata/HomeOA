"""Form 1120-H worksheet. The Treasurer opens it from Money > Reports, sees the income
and spending tests for the fiscal year and the tax, reclassifies an account and saves."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL


def test_treasurer_reviews_the_1120h_worksheet(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/money/reports/")
    page.get_by_test_id("tax-1120h-link").click()
    expect(page.get_by_role("heading", name="Form 1120-H worksheet", level=1)).to_be_visible()
    page.get_by_test_id("tax-year").select_option("2026")
    page.get_by_role("button", name="Show").click()
    expect(page.get_by_test_id("income-test")).to_contain_text("%")
    expect(page.get_by_test_id("tax-due")).to_contain_text("$")
    axe(page)
    form = page.get_by_test_id("tax-classes")
    form.get_by_label("Deductions directly connected with other income").fill("10.00")
    form.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Worksheet saved.")).to_be_visible()
    expect(page.get_by_label("Deductions directly connected with other income")).to_have_value(
        "10.00"
    )
