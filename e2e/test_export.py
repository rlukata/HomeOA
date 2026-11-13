"""Generic export (RPT-01): every {% export_links %} page downloads CSV, XLSX and PDF
named <assoc>-<stem>-<date>.<ext>; CSV and XLSX carry the same numbers (totals included);
PDF is non-empty application/pdf. Clock is fixed at 2026-10-07."""

import csv
import io
import re
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers

DATE = "2026-10-07"
PAGES = [
    ("/money/reports/trial-balance/?as_of=2026-10-07&basis=accrual", "trial-balance", True),
    ("/money/reports/gl/?basis=accrual", "general-ledger", False),
    ("/money/reports/ar-aging/", "receivables-aging", True),
    ("/money/reports/ap-aging/", "payables-aging", True),
    (
        "/money/reports/income-statement/?start=2026-01-01&end=2026-10-07&basis=accrual",
        "income-statement",
        False,
    ),
    ("/money/reports/balance-sheet/?as_of=2026-10-07&basis=accrual", "balance-sheet", False),
    ("/money/reports/cash-flow/?start=2026-01-01&end=2026-10-07&basis=accrual", "cash-flow", False),
    ("/units/", "units", False),
    ("/units/A/", "unit-A-ledger", False),
    ("/units/A/comms/", "unit-A-comms", False),
]
TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


@pytest.fixture
def billed(seed):
    seed_helpers.billing()


def _num(text):
    t = (text or "").strip()
    return Decimal(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


@pytest.mark.parametrize(("path", "stem", "has_totals"), PAGES, ids=[p[1] for p in PAGES])
def test_export_formats(page, billed, login, path, stem, has_totals):
    login(BOARD_EMAIL)
    page.goto(path)
    name_re = re.compile(rf"^[A-Za-z0-9-]+-{re.escape(stem)}-{DATE}\.(csv|xlsx|pdf)$")
    bodies = {}
    for fmt, ctype in TYPES.items():
        link = page.locator(f'a[data-export="{fmt}"]').first
        with page.expect_download() as dl:
            link.click()
        assert name_re.match(dl.value.suggested_filename), dl.value.suggested_filename
        assert dl.value.suggested_filename.endswith("." + fmt)
        resp = page.request.get(page.url.split("?")[0] + link.get_attribute("href"))
        assert resp.status == 200, (fmt, resp.status)
        assert resp.headers["content-type"].startswith(ctype), resp.headers["content-type"]
        bodies[fmt] = resp.body()

    assert bodies["pdf"].startswith(b"%PDF") and len(bodies["pdf"]) > 500
    rows = list(csv.reader(io.StringIO(bodies["csv"].decode("utf-8-sig"))))
    ws = load_workbook(io.BytesIO(bodies["xlsx"]), read_only=True).active
    xrows = [list(r) for r in ws.iter_rows(values_only=True)]
    # XLSX may add a title/subtitle above the header; align on the CSV header row.
    hdr = [str(c) for c in rows[0]]
    start = next(i for i, r in enumerate(xrows) if [str(c or "") for c in r[: len(hdr)]] == hdr)
    xrows = [r for r in xrows[start:] if any(c not in (None, "") for c in r)]
    assert len(xrows) == len(rows), (len(xrows), len(rows))
    for crow, xrow in zip(rows[1:], xrows[1:], strict=True):
        for c, x in zip(crow, xrow, strict=False):
            n = _num(c)
            if n is not None and isinstance(x, int | float):
                assert Decimal(str(round(x, 2))) == n.quantize(Decimal("0.01")), (c, x)
    if has_totals:
        assert rows[-1][0] == "Totals", rows[-1]
        assert all(c == "" or _num(c) is not None for c in rows[-1][1:]), rows[-1]
