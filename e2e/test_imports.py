"""eCondo import and parallel-run diff."""

import re
from pathlib import Path

from playwright.sync_api import expect

from conftest import BOARD_EMAIL

FIXTURES = Path(__file__).parent / "fixtures" / "econdo"  # Sample eCondo-shaped CSVs


def upload_snapshot(page, folder):
    page.get_by_label(re.compile("files?", re.IGNORECASE)).set_input_files(
        sorted(folder.glob("*.csv"))
    )
    page.get_by_role("button", name=re.compile("upload|next", re.IGNORECASE)).click()


def import_base(page):
    """Import and commit the base snapshot into the bare association."""
    page.goto("/units/import/")
    upload_snapshot(page, FIXTURES / "base")
    page.get_by_role("button", name=re.compile("save mapping", re.IGNORECASE)).click()
    page.get_by_role("button", name=re.compile("preview", re.IGNORECASE)).click()
    expect(page.get_by_text(re.compile(r"9 units"))).to_be_visible()
    page.get_by_role("button", name=re.compile("commit|import", re.IGNORECASE)).click()
    expect(page.get_by_text(re.compile("import complete", re.IGNORECASE))).to_be_visible()


def test_econdo_import(page, seed_bare, login):
    login(BOARD_EMAIL)
    import_base(page)
    page.goto("/units/G/")
    expect(page.get_by_test_id("unit-balance")).to_have_text("$726.72")
    page.goto("/units/H/")
    expect(page.get_by_test_id("unit-balance")).to_contain_text("50.00")

    page.goto("/units/")
    expect(page.get_by_role("table").get_by_role("row")).to_have_count(10)

    # Re-import reuses the saved mapping and changes nothing.
    page.goto("/units/import/")
    upload_snapshot(page, FIXTURES / "base")
    expect(page.get_by_role("combobox", name=re.compile("mapping", re.IGNORECASE))).to_have_value(
        re.compile(".+")
    )
    page.get_by_role("button", name=re.compile("preview", re.IGNORECASE)).click()
    expect(page.get_by_text(re.compile(r"0 changes|no changes", re.IGNORECASE))).to_be_visible()


def test_parallel_run_diff(page, seed_bare, login):
    login(BOARD_EMAIL)
    import_base(page)
    # Later snapshot is loaded for comparison only, never committed.
    page.goto("/money/reports/parallel-run/")
    upload_snapshot(page, FIXTURES / "later_unit7_latefee")
    page.get_by_role("button", name=re.compile("run diff|compare", re.IGNORECASE)).click()
    diff = page.get_by_role("table", name=re.compile("owner balances", re.IGNORECASE))
    row = diff.get_by_role("row", name=re.compile(r"\bG\b"))
    expect(row).to_contain_text("$25.00")
    expect(diff.get_by_role("row").filter(has_text="$25.00")).to_have_count(1)
    gl = page.get_by_role("table", name=re.compile("GL|accounts", re.IGNORECASE))
    expect(
        gl.get_by_role("row").filter(has_text=re.compile("late fee", re.IGNORECASE))
    ).to_contain_text("$25.00")
    expect(
        page.get_by_text(re.compile("all other.*matched|8 matched", re.IGNORECASE))
    ).to_be_visible()
