"""ARC forms, submission, review (ARC-01, ARC-02), on unit D. Decision letters,
deadlines and reminders are in test_arc_deadlines.py.

Board: /requests/architectural/ (new/ = submit for a unit, <pk>/ review, forms/).
Owner: /me/arc/ and /me/arc/new/. Seeded form "Exterior change". Reviewers default to
the board roster (one seat per person)."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL
from helpers import m2, m5

ARC = "/requests/architectural/"
TODO_D = "Vote on ARC request Unit D"


def _fill_default_form(page, title="Front door replacement"):
    page.get_by_label("Short title").fill(title)
    page.get_by_label("Describe the change").fill("Replace front door, same color")
    page.get_by_label("Where on the property").fill("Front entry")
    page.get_by_label("Type of change").select_option(label="Windows or doors")


def _submit_on_behalf(page, tmp_path, unit="D", files=True) -> str:
    page.goto(ARC + "new/")
    page.locator("#arc-unit").select_option(label=f"Unit {unit}")
    page.locator("#arc-form").select_option(label="Exterior change")
    page.get_by_role("button", name="Continue").click()
    _fill_default_form(page)
    if files:
        page.get_by_label("Plans, photos or quotes").set_input_files(
            [m5.pdf(tmp_path, "door-spec.pdf"), m5.png(tmp_path, "door.png")]
        )
    page.get_by_role("button", name="Submit request").click()
    expect(page.get_by_text(f"Request submitted for Unit {unit}.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/requests/architectural/\d+/$"))
    expect(page.get_by_test_id("status")).to_contain_text("Submitted")
    return page.url


def _vote(page, url, choice, note=""):
    page.goto(url)
    page.locator("#v-vote").select_option(label=choice)
    if note:
        page.locator("#v-note").fill(note)
    page.get_by_role("button", name="Record vote").click()
    expect(page.get_by_text("Vote recorded.")).to_be_visible()


def test_arc_vote(page, browser, base_url, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    url = _submit_on_behalf(page, tmp_path)
    att = page.get_by_test_id("attachments")
    expect(att).to_contain_text("door-spec.pdf")
    expect(att).to_contain_text("door.png")
    votes = page.get_by_test_id("votes-in")
    expect(votes).to_have_text(re.compile(r"^0 of \d+ votes in$"))
    total = int(re.search(r"of (\d+)", votes.inner_text()).group(1))
    assert total >= 2, total
    axe(page)

    page.goto("/")
    expect(page.get_by_text(TODO_D)).to_be_visible()
    _vote(page, url, "Approve with conditions", "Match existing trim color")
    expect(votes).to_have_text(f"1 of {total} votes in")
    page.goto("/")
    expect(page.get_by_text(TODO_D)).to_have_count(0)  # voter's to-do clears

    ctx = browser.new_context(base_url=base_url)
    sec = ctx.new_page()
    login(SECRETARY_EMAIL, sec)
    sec.goto("/")
    expect(sec.get_by_text(TODO_D)).to_be_visible()
    _vote(sec, url, "Approve")
    expect(sec.get_by_test_id("votes-in")).to_have_text(f"2 of {total} votes in")
    ctx.close()

    # Decision is human; votes never decide on their own.
    page.goto(url)
    expect(page.get_by_test_id("status")).not_to_contain_text("Approved")
    page.locator("#d-out").select_option(label="Approved with conditions")
    page.locator("#d-cond").fill("Door color must match existing trim.")
    page.get_by_role("button", name="Record decision").click()
    expect(page.get_by_text("Decision recorded.")).to_be_visible()
    expect(page.get_by_test_id("status")).to_contain_text("Approved with conditions")
    expect(page.get_by_text("Door color must match existing trim.").first).to_be_visible()
    axe(page)


def test_request_info_and_internal_comments(page, browser, base_url, seed, login, tmp_path):
    m2.residents_enabled(True)
    login(BOARD_EMAIL)
    _submit_on_behalf(page, tmp_path, unit="G", files=False)  # owner7 = unit G
    page.locator("#c-body").fill("Internal: check HOA paint list")
    page.get_by_role("button", name="Add comment").click()
    expect(page.get_by_text("Comment added.")).to_be_visible()
    page.locator("#i-body").fill("Please send the paint color code.")
    page.get_by_role("button", name="Ask the owner").click()
    expect(page.get_by_text("Information requested from the owner.")).to_be_visible()
    expect(page.get_by_test_id("status")).to_contain_text("Information requested")

    ctx = browser.new_context(base_url=base_url)
    owner = ctx.new_page()
    login(OWNER_EMAIL, owner)
    owner.goto("/me/arc/")
    owner.get_by_role("link", name=re.compile("Front door replacement")).first.click()
    comments = owner.get_by_test_id("comments")
    expect(comments).to_contain_text("Please send the paint color code.")
    expect(comments).not_to_contain_text("Internal: check HOA paint list")
    ctx.close()


def test_resident_submit_owner_only(page, browser, base_url, seed, login):
    m2.residents_enabled(True)
    login(OWNER_EMAIL)
    page.goto("/me/arc/new/")
    _fill_default_form(page, "New window boxes")
    page.get_by_role("button", name="Send to the board").click()
    expect(page).to_have_url(re.compile(r"/me/arc/\d+/$"))
    expect(page.get_by_test_id("status")).to_contain_text("Submitted")

    ctx = browser.new_context(base_url=base_url)
    t = ctx.new_page()
    login(TENANT_EMAIL, t)
    r = t.request.get("/me/arc/new/", max_redirects=0)
    assert r.status in (302, 403, 404), r.status  # Tenants have no ARC
    ctx.close()


def test_form_builder_required_field(page, seed, login):
    login(BOARD_EMAIL)
    page.goto(ARC + "forms/")
    page.get_by_role("link", name="New form").click()
    page.get_by_label("Form name").fill("Satellite dish")
    rows = page.get_by_test_id("field-rows").locator("tbody tr")
    rows.first.get_by_label("Label").fill("Mount location")
    rows.first.get_by_label("Type").select_option(label="Short text")
    rows.first.get_by_label("Required").check()
    page.get_by_role("button", name="Save form").click()
    expect(page.get_by_text("Form saved.")).to_be_visible()

    page.goto(ARC + "new/")
    page.locator("#arc-unit").select_option(label="Unit D")
    page.locator("#arc-form").select_option(label="Satellite dish")
    page.get_by_role("button", name="Continue").click()
    page.get_by_label("Short title").fill("Dish on balcony")
    field = page.get_by_label(re.compile("^Mount location"))
    expect(field).to_have_attribute("required", "")
    # Bypass browser validation: the server must refuse a missing required answer.
    field.evaluate("e => e.removeAttribute('required')")
    page.get_by_role("button", name="Submit request").click()
    expect(page).not_to_have_url(re.compile(r"/requests/architectural/\d+/$"))
    expect(page.locator(".field-err").first).to_be_visible()
