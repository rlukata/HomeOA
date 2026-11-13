"""Fine schedule + violation log with photos (VIO-01, VIO-02).

Pages under /requests/violations/: log + filters + "Log a violation" form (index),
<pk>/ detail, categories/, schedules/ and schedules/<pk>/ (dates + fine lines).
Pilot unit I. Fixed clock Oct 7. Escalation, hearing and fine
steps are in test_escalation.py and test_hearings.py."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, TENANT_EMAIL
from helpers import m2, m5

V = "/requests/violations/"
CAT = "Trash bins left out"
BANNER = "Not yet furnished to owners: fines cannot be imposed"


def _category(page, name=CAT, cite="Rules §4.2 Trash"):
    page.goto(V + "categories/")
    form = page.locator("form").filter(has=page.get_by_role("button", name="Add category"))
    form.get_by_label("Name").fill(name)
    form.get_by_label("Rule citation").fill(cite)
    form.get_by_role("button", name="Add category").click()
    expect(page.get_by_text("Category added.")).to_be_visible()


def _schedule(page, furnished: str | None):
    page.goto(V + "schedules/")
    page.get_by_label("Adopted on", exact=True).fill("2026-08-20")
    page.get_by_label("Effective date", exact=True).fill("2026-09-01")
    if furnished:
        page.get_by_label("Furnished to owners on").fill(furnished)
    page.get_by_role("button", name="Add version").click()
    expect(page.get_by_text("Fine schedule version added.")).to_be_visible()
    page.get_by_role("combobox", name="Category").select_option(label=CAT)
    page.get_by_label("First offense ($)").fill("50.00")
    page.get_by_label("Repeat offense ($)").fill("100.00")
    page.get_by_label("Continuing fine ($)").fill("10.00")
    page.get_by_label("Continuing period (days)").fill("7")
    page.get_by_role("button", name="Save fine line").click()
    expect(page.get_by_text("Fine line saved.")).to_be_visible()


def _fill_log(page, files, unit="I", desc="Bins left at curb for 3 days"):
    page.goto(V)
    page.locator("select[name=unit]").last.select_option(label=f"Unit {unit}")
    page.get_by_role("combobox", name="Category").select_option(label=CAT)
    page.get_by_label("Observed on").fill("2026-10-06")
    page.get_by_label("Description").fill(desc)
    page.get_by_label("Photos (PNG or JPEG)").set_input_files(files)
    page.get_by_role("button", name="Log violation").click()


def _log(page, tmp_path, unit="I"):
    _fill_log(page, m5.png(tmp_path, "bins.png"), unit)
    expect(page.get_by_text("Violation logged.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/requests/violations/\d+/$"))


def test_unfurnished_schedule_warns(page, seed, login, axe):
    login(BOARD_EMAIL)
    _category(page)
    _schedule(page, furnished=None)
    expect(page.get_by_text(BANNER)).to_be_visible()
    axe(page)


def test_log_violation_with_photo(page, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    _category(page)
    _schedule(page, furnished="2026-08-25")
    expect(page.get_by_text(BANNER)).to_have_count(0)
    _log(page, tmp_path)
    expect(page.get_by_test_id("status")).to_have_text("Open")
    expect(page.get_by_test_id("photos").locator("img")).to_have_count(1)
    expect(page.get_by_text("Rules §4.2 Trash")).to_be_visible()
    expect(page.get_by_text(BANNER)).to_have_count(0)
    axe(page)

    # A non-image "photo" is refused and nothing is logged.
    _fill_log(page, m5.pdf(tmp_path), desc="pdf instead of photo")
    expect(page).to_have_url(re.compile(r"/requests/violations/$"))
    expect(page.get_by_text("pdf instead of photo")).to_have_count(0)
    expect(page.get_by_test_id("violations").locator("tbody tr")).to_have_count(1)

    # Home to-do and list filters.
    page.goto("/")
    expect(page.get_by_text("1 open violation", exact=True)).to_be_visible()
    page.goto(V)
    rows = page.get_by_test_id("violations").locator("tbody tr")
    expect(rows).to_have_count(1)
    page.locator("select[name=status]").select_option(label="Cured")
    page.get_by_role("button", name="Filter").click()
    expect(page.get_by_text("No violations match.")).to_be_visible()
    page.locator("select[name=status]").select_option(label="All")
    page.locator("select[name=unit]").first.select_option(label="Unit I")
    page.get_by_role("button", name="Filter").click()
    expect(rows).to_have_count(1)
    page.locator("select[name=unit]").first.select_option(label="Unit A")
    page.get_by_role("button", name="Filter").click()
    expect(page.get_by_text("No violations match.")).to_be_visible()
    axe(page)


def test_status_change_audited(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _category(page)
    _schedule(page, furnished="2026-08-25")
    _log(page, tmp_path)
    page.locator("select[name=status]").select_option(label="Dismissed")
    status_form = page.locator("form").filter(has=page.get_by_role("button", name="Save status"))
    status_form.get_by_label("Note").fill("Bins moved same day")
    page.get_by_role("button", name="Save status").click()
    expect(page.get_by_text("Status updated.")).to_be_visible()
    expect(page.get_by_test_id("status")).to_have_text("Dismissed")
    page.goto("/settings/audit/?object_type=enforcement.violation")
    expect(page.locator("tbody tr").filter(has_text="Violation")).not_to_have_count(0)
    expect(page.get_by_text("No audit events match.")).to_have_count(0)
    page.goto("/")
    expect(page.get_by_text(re.compile(r"open violation"))).to_have_count(0)


def test_residents_cannot_see_violations(seed, browser, base_url, login):
    m2.residents_enabled(True)
    for email in (OWNER_EMAIL, TENANT_EMAIL):
        ctx = browser.new_context(base_url=base_url)
        p = ctx.new_page()
        login(email, p)
        r = p.request.get(V, max_redirects=0)
        assert r.status in (302, 403, 404), (email, r.status)
        ctx.close()
