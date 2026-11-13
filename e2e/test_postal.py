"""Mail batches (print-locally default postal provider) and their axe scans."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, FIXED_TODAY
from helpers.m2 import residents_enabled


def _generate_letter(page) -> None:
    """Generate one letter from the first seeded template (seed has none generated)."""
    page.goto("/settings/letters/")
    page.locator("table a").first.click()
    expect(page).to_have_url(re.compile(r"/settings/letters/\d+/"))
    unit = page.locator("#id_unit")
    unit.select_option(index=1)
    page.locator("#id_as_of").fill(FIXED_TODAY)
    page.get_by_role("button", name="Generate PDF").click()
    generated = page.get_by_role("table", name="Generated letters")
    expect(generated.locator("tbody tr a")).to_have_count(1)


def _draft_batch(page, axe) -> None:
    page.goto("/settings/letters/batches/")
    page.locator("input[name=letters]").first.check()
    page.locator("#id_name").fill("E2E batch")
    page.get_by_role("button", name="Create draft batch").click()
    expect(page).to_have_url(re.compile(r"/settings/letters/batches/\d+/$"))
    expect(page.get_by_role("heading", name="E2E batch")).to_be_visible()
    items = page.get_by_role("table", name="Letters in this batch")
    expect(items.locator("tbody tr")).to_have_count(1)
    axe(page)


def test_mail_batch_stays_draft_while_residents_off(page, seed, login, axe):
    residents_enabled(False)
    login(BOARD_EMAIL)
    resp = page.goto("/settings/letters/batches/")
    assert resp and resp.status == 200, f"batches returned {resp and resp.status}"
    expect(page.get_by_text("No mail batches yet.")).to_be_visible()
    axe(page)
    _generate_letter(page)
    _draft_batch(page, axe)
    expect(page.get_by_text("this batch stays a draft")).to_be_visible()
    expect(page.get_by_role("button", name="Queue for printing")).to_have_count(0)


def test_mail_batch_print_flow(page, seed, login, axe):
    residents_enabled(True)
    login(BOARD_EMAIL)
    _generate_letter(page)
    _draft_batch(page, axe)
    page.get_by_role("button", name="Queue for printing").click()
    expect(page.get_by_text("Batch ready to print.")).to_be_visible()
    pdf = page.get_by_role("link", name=re.compile(r"Print file \(PDF"))
    r = page.request.get(pdf.get_attribute("href"))
    assert r.ok and r.body()[:4] == b"%PDF", f"print file: {r.status}"
    csv = page.get_by_role("link", name="Manifest (CSV)")
    r = page.request.get(csv.get_attribute("href"))
    assert r.ok and b"," in r.body(), f"manifest: {r.status}"
    axe(page)
    page.get_by_role("button", name="Mark printed and mailed").click()
    expect(page.get_by_role("button", name="Mark printed and mailed")).to_have_count(0)
    axe(page)
