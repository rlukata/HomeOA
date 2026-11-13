"""Association setup wizard: Settings > Association setup > a section, the
suggested answers, the draft resolution, adoption, and the policy in force."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL


def test_setup_section_becomes_a_resolution_and_applies_on_adoption(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/settings/")
    page.get_by_test_id("settings-setup").click()
    expect(page.get_by_test_id("setup-progress")).to_contain_text("0 of 5 sections adopted")
    page.get_by_role("link", name="Approvals and limits").first.click()
    form = page.get_by_test_id("setup-form")
    expect(form.get_by_role("radio", name="$1,000 or more")).to_be_checked()
    axe(page)
    form.get_by_role("radio", name="$500 or more").check()
    page.get_by_role("button", name="Draft the resolution").click()

    expect(page.get_by_text("The settings apply when the board adopts it.")).to_be_visible()
    expect(page.locator("h1")).to_contain_text("Policy: Approvals and limits")
    res_url = page.url
    # The Secretary adopts resolutions.
    login(SECRETARY_EMAIL)
    page.goto(res_url)
    page.get_by_role("button", name="Adopt resolution").click()
    page.goto(res_url)
    expect(page.get_by_test_id("status")).to_contain_text("Adopted")

    page.goto("/settings/setup/")
    expect(page.get_by_test_id("setup-progress")).to_contain_text("1 of 5 sections adopted")
    row = page.locator("[data-testid=setup-section][data-key=approvals]")
    expect(row).to_contain_text("Adopted")
    page.goto("/money/bills/approval-rules/")
    expect(page.get_by_text("Setup: bills of $500.00 or more")).to_be_visible()


def test_reserve_withdrawal_goes_to_a_second_board_member(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/money/bank/")
    page.get_by_role("link", name="Reserve withdrawals").click()
    axe(page)
    page.get_by_label("Amount").fill("1200.00")
    page.get_by_label("Purpose").fill("Roof repair")
    page.get_by_role("button", name="Send for approval").click()
    expect(page.get_by_text("Sent for board approval")).to_be_visible()
    expect(page.get_by_text("Approve a reserve withdrawal: $1200.00")).to_be_visible()


def test_turning_a_feature_off_hides_its_pages_once_adopted(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/settings/setup/")
    page.locator("[data-testid=setup-section][data-key=features] a").first.click()
    expect(page.get_by_test_id("setup-always-on")).to_contain_text("Records requests")
    axe(page)
    form = page.get_by_test_id("setup-form")
    form.get_by_role("group", name="Calendar", exact=True).get_by_role("checkbox").uncheck()
    page.get_by_role("button", name="Draft the resolution").click()
    expect(page.locator("h1")).to_contain_text("Policy: Features")
    res_url = page.url

    login(SECRETARY_EMAIL)
    page.goto(res_url)
    page.get_by_role("button", name="Adopt resolution").click()
    page.goto(res_url)
    expect(page.get_by_test_id("status")).to_contain_text("Adopted")

    response = page.goto("/community/calendar/")
    assert response is not None and response.status == 404


def test_a_legal_limit_shows_a_slider_that_follows_the_exact_amount(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/settings/setup/")
    page.get_by_role("link", name="Records, meetings and notices").first.click()
    fee = page.get_by_role("group", name="Fee for a resale certificate")
    expect(fee.get_by_test_id("setup-bounds")).to_contain_text("RCW 64.34.425(2)")
    slider = fee.get_by_role("slider")
    expect(slider).to_be_visible()
    axe(page)
    fee.get_by_label("Exact amount").fill("150")
    expect(slider).to_have_value("150")
