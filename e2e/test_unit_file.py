"""Board unit file (DOC-02) with kind filters; owners get 403."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2
from helpers import seed as seed_helpers


def test_unit_file_filters_and_owner_403(page, seed, login, axe):
    seed_helpers.billing()
    login(BOARD_EMAIL)
    # File a statement so the unit file has more than the ledger in it.
    page.goto("/money/statements/")
    page.get_by_role("button", name="Generate statements for October 2026").click()
    expect(page).to_have_url(re.compile(r"/money/statements/\d+/$"))

    resp = page.goto("/units/C/file/")
    assert resp and resp.status == 200, f"unit file returned {resp and resp.status}"
    rows = page.get_by_test_id("unit-file").locator("tbody tr[data-kind]")
    expect(rows.first).to_be_visible()
    total = rows.count()
    assert "statements" in set(rows.evaluate_all("rs => rs.map(r => r.dataset.kind)"))
    axe(page)

    filters = page.get_by_role("navigation", name="Filter unit file").get_by_role("link")
    keys = [
        h.split("kind=")[1]
        for h in filters.evaluate_all("as => as.map(a => a.href)")
        if "kind=" in h
    ]
    seen = 0
    for key in keys:
        page.goto(f"/units/C/file/?kind={key}")
        kinds = set(rows.evaluate_all("rs => rs.map(r => r.dataset.kind)"))
        assert kinds <= {key}, f"filter {key} showed {kinds}"
        seen += rows.count()
    assert seen == total, f"filtered rows {seen} != all {total}"
    axe(page)

    m2.residents_enabled(True)
    page.context.clear_cookies()
    login(m2.OWNERC_EMAIL)
    resp = page.goto("/units/C/file/")
    assert resp and resp.status == 403, f"owner got {resp and resp.status}"


def test_unit_file_ledger_export(page, seed, login, axe):
    seed_helpers.billing()
    login(BOARD_EMAIL)
    page.goto("/units/G/file/")
    page.get_by_test_id("ledger-export").click()
    expect(page).to_have_url(re.compile(r"/units/G/file/"))
    row = page.get_by_test_id("unit-file").locator('tbody tr[data-kind="ledger_exports"]')
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Itemized ledger")
    axe(page)

    pdf = page.request.get(
        row.get_by_role("link", name=re.compile(r"\(PDF\)")).get_attribute("href")
    )
    assert pdf.status == 200 and pdf.body()[:4] == b"%PDF", pdf.status
    csv = page.request.get(row.get_by_role("link", name="CSV").get_attribute("href"))
    assert csv.status == 200, csv.status
    lines = csv.text().strip().splitlines()
    assert len(lines) >= 2 and "," in lines[0], lines[:2]  # header + at least the October dues
