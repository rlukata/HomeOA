"""Owner statements (FIN-04): board generates October; preview-only while residents off."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.m2 import residents_enabled


def test_generate_october_statements_preview(page, seed, login, axe):
    residents_enabled(False)
    login(BOARD_EMAIL)
    resp = page.goto("/money/statements/")
    assert resp and resp.status == 200, f"statements returned {resp and resp.status}"
    expect(page.get_by_test_id("preview-mode")).to_be_visible()
    axe(page)

    page.get_by_role("button", name="Generate statements for October 2026").click()
    expect(page).to_have_url(re.compile(r"/money/statements/\d+/$"))
    expect(page.get_by_role("heading", name=re.compile("October 2026"))).to_be_visible()
    expect(page.get_by_test_id("preview-mode")).to_be_visible()
    rows = page.get_by_test_id("statements").locator("tbody tr")
    expect(rows).to_have_count(9)  # units A-I
    axe(page)
    run_url = page.url

    # Scenario 6 (preview stage): every unit is routed to Email or Mail and nothing is sent.
    table = page.get_by_test_id("statements")
    channels = table.get_by_test_id("channel").all_inner_texts()
    assert len(channels) == 9 and set(c.strip() for c in channels) <= {"Email", "Mail"}, channels
    statuses = table.get_by_test_id("status").all_inner_texts()
    assert all(s.strip() == "Preview only" for s in statuses), statuses

    # Each PDF opens for the board.
    href = rows.first.locator("a[href$='.pdf']").first.get_attribute("href")
    pdf = page.request.get(href)
    assert pdf.status == 200 and pdf.body()[:4] == b"%PDF", f"{href} -> {pdf.status}"

    # Regenerating is idempotent: still one run, still 9 rows.
    page.get_by_role("button", name="Regenerate").click()
    expect(page).to_have_url(run_url)
    expect(page.get_by_test_id("statements").locator("tbody tr")).to_have_count(9)
