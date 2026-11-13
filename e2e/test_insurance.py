"""Master insurance policy. The Secretary sees it on Documents > Insurance and adds the next
term; an owner sees the carrier, term and deductible on Docs, not the premium; the renewal
is on the compliance calendar. Synthetic seed data only."""

from playwright.sync_api import expect

from conftest import OWNER_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL
from test_portal import billed, residents_on  # noqa: F401


def test_secretary_records_the_next_term(page, residents_on, login, axe):  # noqa: F811
    login(SECRETARY_EMAIL)
    page.goto("/documents/")
    page.get_by_role("link", name="Insurance", exact=True).click()
    policy = page.get_by_test_id("master-policy")
    expect(policy).to_contain_text("Evergreen Mutual")
    expect(page.get_by_test_id("policy-term")).to_contain_text("Mar 1, 2027")
    axe(page)
    page.goto("/governance/compliance/")
    expect(page.locator('tr[data-kind="insurance"]')).to_contain_text("Evergreen Mutual")
    page.goto("/documents/insurance/")
    page.get_by_test_id("policy-add").click()
    form = page.get_by_test_id("policy-form")
    form.get_by_label("Insurance company").fill("Cascade Insurance")
    form.get_by_label("Term start").fill("2027-03-01")
    form.get_by_label("Term end").fill("2028-03-01")
    form.get_by_label("Deductible").fill("15000.00")
    axe(page)
    form.get_by_role("button", name="Save policy").click()
    expect(page.get_by_text("Policy saved.")).to_be_visible()
    expect(page.get_by_text("All terms")).to_be_visible()


def test_owner_sees_summary_tenant_does_not(page, residents_on, login, axe):  # noqa: F811
    login(OWNER_EMAIL)
    page.goto("/me/docs/")
    summary = page.get_by_test_id("owner-insurance")
    expect(summary).to_contain_text("Evergreen Mutual")
    expect(summary).to_contain_text("$10,000.00")
    expect(summary).not_to_contain_text("$10,717.00")
    axe(page)
    page.context.clear_cookies()
    login(TENANT_EMAIL)
    page.goto("/me/docs/")
    expect(page.get_by_test_id("owner-insurance")).to_have_count(0)
