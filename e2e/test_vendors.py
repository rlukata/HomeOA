"""Vendors: list search/filter, add/edit vendor and contact, TIN masking, access.

Adding a vendor, deactivating it and changing its tax details (W-9) are change requests:
the treasurer proposes and the secretary approves. Contacts and plain fields are direct
edits for any board member."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL
from helpers.flows import propose_and_approve

GREENWAY = "/vendors/greenway-landscaping/"


def rows(page):
    return page.locator("#vendor-rows tr[data-testid^='vendor-row-']")


def test_list_search_and_filter(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/vendors/")
    expect(rows(page)).to_have_count(7)
    axe(page)
    page.get_by_test_id("vendor-search").fill("gutter")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("Evergreen Gutter & Roof")
    page.get_by_test_id("vendor-search").fill("Luis")  # contact name
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("Greenway Landscaping")
    page.get_by_test_id("vendor-search").fill("zzz-none")
    expect(page.get_by_text("No vendors match.")).to_be_visible()
    page.get_by_test_id("vendor-search").fill("")
    expect(rows(page)).to_have_count(7)
    # Inactive filter: none yet.
    page.locator("label.chip", has_text="Inactive").click()
    expect(page.get_by_text("No vendors match.")).to_be_visible()


def _approved(page):
    """The secretary approves the request; follow it to the vendor."""
    propose_and_approve(page)
    page.get_by_role("link", name="See the result").click()


def test_add_edit_vendor_and_contact(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/vendors/")
    page.get_by_role("link", name="Add vendor").click()
    axe(page)
    page.locator("#id_name").fill("Rainier Plumbing")
    page.locator("#id_tin_input").fill("12-3456789")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_test_id("change-details")).to_contain_text("Rainier Plumbing")
    expect(page.get_by_test_id("change-details")).not_to_contain_text("345")
    axe(page)
    _approved(page)
    expect(page.get_by_role("heading", name="Rainier Plumbing", level=1)).to_be_visible()
    expect(page.get_by_test_id("vendor-tin")).to_contain_text("6789")
    expect(page.get_by_test_id("vendor-tin")).not_to_contain_text("345")
    axe(page)

    page.get_by_role("link", name="Add contact").click()
    page.locator("#id_name").fill("Pat Rivera")
    page.locator("#id_email").fill("pat@rainier.example")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_test_id("vendor-contact")).to_contain_text("Pat Rivera")
    page.get_by_test_id("vendor-contact").get_by_role("link", name="Edit").click()
    page.locator("#id_role").fill("Dispatcher")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_test_id("vendor-contact")).to_contain_text("Dispatcher")

    page.get_by_role("link", name="Edit", exact=True).first.click()
    page.locator("#id_status").select_option("inactive")
    page.get_by_role("button", name="Save").click()  # blank TIN keeps the old one
    _approved(page)
    expect(page.get_by_test_id("vendor-tin")).to_contain_text("6789")
    page.goto("/vendors/")
    page.locator("label.chip", has_text="Inactive").click()
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("Rainier Plumbing")


def test_tin_masked_then_revealed(page, seed, login):
    login(BOARD_EMAIL)
    page.goto(GREENWAY)
    tin = page.get_by_test_id("vendor-tin")
    expect(tin).to_contain_text(re.compile(r"\*+-\*+-?0101"))
    tin.get_by_test_id("reveal-tin").click()
    expect(page.get_by_test_id("vendor-tin")).to_contain_text(
        re.compile(r"000-?00-?0101|00-?0000101")
    )
    expect(page.get_by_test_id("vendor-tin")).not_to_contain_text("*")


def test_secretary_edits_but_cannot_see_tin(page, seed, login):
    """Every board member edits vendors (sensitive changes go for approval); only the
    treasurer reveals a TIN."""
    login(SECRETARY_EMAIL)
    page.goto("/vendors/")
    expect(rows(page)).to_have_count(7)
    expect(page.get_by_role("link", name="Add vendor")).to_be_visible()
    page.goto(GREENWAY)
    expect(page.get_by_role("link", name="Edit", exact=True).first).to_be_visible()
    expect(page.get_by_role("link", name="Add contact")).to_be_visible()
    expect(page.get_by_test_id("reveal-tin")).to_have_count(0)
    r = page.request.post(GREENWAY + "tin/")
    assert r.status == 403, r.status


def test_resident_gets_403(page, seed, login):
    login(OWNER_EMAIL)
    for path in ["/vendors/", GREENWAY, GREENWAY + "w9/"]:
        resp = page.goto(path)
        assert resp and resp.status == 403, f"{path} -> {resp and resp.status}"


def test_w9_download_treasurer_only(page, seed, login):
    login(BOARD_EMAIL)
    page.goto(GREENWAY + "edit/")
    page.locator("#id_w9_file").set_input_files(
        {"name": "w9.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF\n"}
    )
    page.get_by_role("button", name="Save").click()
    _approved(page)
    link = page.get_by_test_id("vendor-w9")
    expect(link).to_be_visible()
    r = page.request.get(link.get_attribute("href"))
    assert r.status == 200 and r.body()[:4] == b"%PDF", r.status
    page.context.clear_cookies()

    login(SECRETARY_EMAIL)
    page.goto(GREENWAY)
    expect(page.get_by_test_id("vendor-w9")).to_have_count(0)
    expect(page.locator("dd", has_text="On file")).to_be_visible()
    resp = page.goto(GREENWAY + "w9/")
    assert resp and resp.status == 403, resp and resp.status
