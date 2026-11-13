"""Statement exports (RPT-01 on the financial statements): export links keep the period,
as_of, basis and fund filters; CSV summary rows match the on-screen totals; the strict-cash
per-fund cash note is a footer row on cash per-fund exports only; owners refused."""

import csv
import io
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import seed as seed_helpers
from test_export import _num
from test_financial_statements import BS, CF, IS, _money

NOTE = "Per-fund cash figures are net of interfund balances"
RANGE = "start=2026-01-01&end=2026-10-07"
CASES = [
    (IS, RANGE, ("start", "end")),
    (BS, "as_of=2026-10-07", ("as_of",)),
    (CF, RANGE, ("start", "end")),
]


@pytest.fixture
def billed(seed):
    seed_helpers.billing()


def _csv(page, href):
    resp = page.request.get(page.url.split("?")[0] + href)
    assert resp.status == 200, (href, resp.status)
    return list(csv.reader(io.StringIO(resp.body().decode("utf-8-sig"))))


@pytest.mark.parametrize("basis", ["accrual", "cash"])
@pytest.mark.parametrize(("url", "query", "keys"), CASES, ids=["is", "bs", "cf"])
def test_statement_export_matches_screen(page, billed, login, url, query, keys, basis):
    login(BOARD_EMAIL)
    page.goto(f"{url}?{query}&basis={basis}")
    hrefs = {}
    for fmt in ("csv", "xlsx", "pdf"):
        href = page.locator(f'a[data-export="{fmt}"]').first.get_attribute("href")
        q = parse_qs(urlparse(href).query)
        assert q["format"] == [fmt] and q["basis"] == [basis], href
        for k in keys:
            assert k in q, (k, href)
        hrefs[fmt] = href
    rows = _csv(page, hrefs["csv"])
    by_label = {r[0]: r[1:] for r in rows[1:]}

    summary = page.locator("[data-testid=statement] tfoot tr")
    assert summary.count() > 0
    for i in range(summary.count()):
        tr = summary.nth(i)
        label = tr.locator("th").inner_text().strip()
        screen = [_money(t) for t in tr.locator("td").all_inner_texts()]
        assert label in by_label, (label, list(by_label))
        exported = [_num(c) or 0 for c in by_label[label]]
        assert exported == screen, (label, exported, screen)

    has_note = any(r[0].startswith(NOTE) for r in rows)
    assert has_note == (basis == "cash"), (basis, rows[-1])
    if has_note:
        assert rows[-1][0].startswith(NOTE) and all(c == "" for c in rows[-1][1:]), rows[-1]


def test_cash_total_only_has_no_note(page, billed, login):
    login(BOARD_EMAIL)
    page.goto(f"{BS}?as_of=2026-10-07&basis=cash&fund=all")
    rows = _csv(page, page.locator('a[data-export="csv"]').first.get_attribute("href"))
    assert not any(r[0].startswith(NOTE) for r in rows)


@pytest.mark.parametrize("url", [IS, BS, CF])
@pytest.mark.parametrize("fmt", ["csv", "xlsx", "pdf"])
def test_owner_refused_export(page, seed, login, url, fmt):
    login(OWNER_EMAIL)
    resp = page.goto(f"{url}?format={fmt}")
    assert resp.status == 403, (url, fmt, resp.status)
