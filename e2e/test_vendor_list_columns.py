"""The Vendors list shows Contract, Insurance certificate and Paid this year
columns, an "Expired <date>" certificate badge and a "License expired" flag; search and
the status filter keep the new columns. Today is 2026-10-07 (fixed clock)."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from test_vendor_docs import GREENWAY, _add_coi, _add_contract

SLUG = "greenway-landscaping"


def _add_license(page, expiry: str):
    page.goto(GREENWAY)
    page.get_by_role("link", name="Add certificate").click()
    page.locator("#id_kind").select_option("license")
    page.locator("#id_issuer").fill("WA L&I")
    page.locator("#id_number").fill("LIC-1")
    page.locator("#id_effective_date").fill("2024-01-01")
    page.locator("#id_expiry_date").fill(expiry)
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Certificate saved.")).to_be_visible()


def test_new_columns_empty_then_filled(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/vendors/")
    head = page.locator("table.t thead")
    for name in ("Contract", "Insurance certificate", "Paid this year"):
        expect(head.get_by_role("columnheader", name=name, exact=True)).to_be_visible()
    expect(page.get_by_test_id(f"vendor-contract-{SLUG}")).to_have_text("—")
    expect(page.get_by_test_id(f"vendor-coi-{SLUG}")).to_have_text("None on file")
    expect(page.get_by_test_id(f"vendor-paid-{SLUG}")).to_have_text("$0.00")
    expect(page.get_by_test_id(f"vendor-license-expired-{SLUG}")).to_have_count(0)

    _add_contract(page, end="2026-12-31", notice_days="30")
    _add_coi(page, expiry="2027-06-30")
    page.goto("/vendors/")
    expect(page.get_by_test_id(f"vendor-contract-{SLUG}")).to_contain_text(
        "Auto-renews Dec 31, 2026"
    )
    expect(page.get_by_test_id(f"vendor-coi-{SLUG}")).to_contain_text("Expires Jun 30, 2027")
    expect(page.get_by_test_id(f"vendor-expired-{SLUG}")).to_have_count(0)
    axe(page)


def test_license_expired_flag(page, seed, login, axe):
    login(BOARD_EMAIL)
    _add_license(page, expiry="2026-09-01")
    page.goto("/vendors/")
    flag = page.get_by_test_id(f"vendor-license-expired-{SLUG}")
    expect(flag).to_have_text("License expired")
    expect(page.locator("[data-testid^='vendor-license-expired-']")).to_have_count(1)
    # A license is not an insurance certificate.
    expect(page.get_by_test_id(f"vendor-coi-{SLUG}")).to_have_text("None on file")
    axe(page)


def test_search_and_filter_keep_columns(page, seed, login):
    login(BOARD_EMAIL)
    _add_coi(page, expiry="2026-09-30")
    page.goto("/vendors/")
    page.get_by_test_id("vendor-search").fill("Greenway")
    rows = page.locator("#vendor-rows tr[data-testid^='vendor-row-']")
    expect(rows).to_have_count(1)
    expect(page.get_by_test_id(f"vendor-expired-{SLUG}")).to_contain_text("Expired Sep 30, 2026")
    expect(page.get_by_test_id(f"vendor-paid-{SLUG}")).to_have_text("$0.00")
    page.get_by_test_id("vendor-search").fill("no-such-vendor-zzz")
    expect(rows).to_have_count(0)
    expect(page.get_by_text("No vendors match.")).to_be_visible()
    page.get_by_test_id("vendor-search").fill("")
    page.locator("label.chip", has_text="All").click()
    expect(page.get_by_test_id(f"vendor-expired-{SLUG}")).to_be_visible()
