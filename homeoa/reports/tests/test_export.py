# ruff: noqa: F811  (fixtures imported from test_reports are used as arguments)
import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from homeoa.audit.models import AuditEvent
from homeoa.reports import export, queries
from homeoa.reports.export import DATE, MONEY, NUMBER, Column, TableSpec

from .test_reports import assoc, books, today, treasurer  # noqa: F401  (fixtures)

D = Decimal


def spec():
    return TableSpec(
        title="Café dues – ünits",
        subtitle="Operating",
        columns=[
            Column("Unit"),
            Column("Date", DATE),
            Column("Share", NUMBER),
            Column("Amount", MONEY),
        ],
        rows=[
            ["A", dt.date(2026, 10, 1), D("11.11"), D("1234.50")],
            ["=HYPERLINK(1)", dt.date(2026, 10, 2), 3, D("-0.25")],
            ["Zoë", None, None, None],
        ],
        totals=["Totals", None, None, D("1234.25")],
    )


def csv_rows(body: bytes):
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


def xlsx_rows(body: bytes):
    ws = load_workbook(io.BytesIO(body)).active
    return [list(r) for r in ws.iter_rows(values_only=True)]


def pdf_text(body: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(body)).pages)


def test_one_spec_three_formats_same_totals():
    s = spec()
    rows = csv_rows(export.render(s, export.CSV))
    assert rows[0] == ["Unit", "Date", "Share", "Amount"]
    assert rows[1] == ["A", "2026-10-01", "11.11", "1234.50"]
    assert rows[2][0] == "'=HYPERLINK(1)"
    assert rows[-1] == ["Totals", "", "", "1234.25"]

    x = xlsx_rows(export.render(s, export.XLSX))
    assert x[0][0] == "Café dues – ünits"
    header = x.index(["Unit", "Date", "Share", "Amount"])
    body = x[header + 1 :]
    assert body[0][3] == 1234.5 and isinstance(body[0][3], float)  # number, not text
    assert body[0][2] == 11.11 and body[1][2] == 3
    assert body[0][1] == dt.datetime(2026, 10, 1)
    assert body[1][0] == "'=HYPERLINK(1)"
    assert body[-1] == ["Totals", None, None, 1234.25]
    assert D(str(body[-1][3])) == D(rows[-1][3])

    text = pdf_text(export.render(s, export.PDF))
    assert "Café dues – ünits" in text and "Zoë" in text  # Unicode font
    assert "1,234.25" in text and "1,234.50" in text


def test_filename_has_association_and_date(assoc):
    name = export.filename(spec(), "xlsx", association=assoc, on=dt.date(2026, 10, 8))
    assert name == "CR-Caf-dues-nits-2026-10-08.xlsx"
    s = TableSpec("Trial balance", [Column("x")], filename="trial-balance")
    assert export.filename(s, "pdf") == "trial-balance.pdf"
    with pytest.raises(ValueError):
        export.render(s, "doc")


def _get(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, url
    return resp


def test_report_exports_match_screen_totals(books, client, treasurer, today):
    client.force_login(treasurer)
    tb = queries.trial_balance(association=books)
    base = "/money/reports/trial-balance/?basis=accrual"
    resp = _get(client, base + "&format=csv")
    assert resp["Content-Type"].startswith("text/csv")
    assert 'filename="CR-trial-balance-' in resp["Content-Disposition"]
    total_csv = csv_rows(resp.content)[-1]
    assert total_csv[0] == "Totals" and D(total_csv[3]) == tb.total_debit == D(total_csv[4])

    resp = _get(client, base + "&format=xlsx")
    assert resp["Content-Disposition"].endswith('.xlsx"')
    last = xlsx_rows(resp.content)[-1]
    assert D(str(last[3])) == tb.total_debit and D(str(last[4])) == tb.total_credit

    resp = _get(client, base + "&format=pdf")
    assert resp.content.startswith(b"%PDF")
    assert f"{tb.total_debit:,.2f}" in pdf_text(resp.content)

    # Aging and GL export too; the AR aging total matches the query.
    ar = queries.ar_aging(as_of=dt.date(2026, 10, 31), association=books)
    for fmt in export.FORMATS:
        _get(client, f"/money/reports/ar-aging/?as_of=2026-10-31&format={fmt}")
        _get(client, f"/money/reports/ap-aging/?as_of=2026-10-31&format={fmt}")
        _get(client, f"/money/reports/gl/?start=2026-09-01&format={fmt}")
    last = csv_rows(_get(client, "/money/reports/ar-aging/?as_of=2026-10-31&format=csv").content)
    assert D(last[-1][-2]) == ar.total


def test_pii_exports_are_audited(books, client, treasurer, today):
    client.force_login(treasurer)
    _get(client, "/money/reports/trial-balance/?format=xlsx")
    assert not AuditEvent.objects.filter(action="reports.exported").exists()
    _get(client, "/money/reports/ar-aging/?as_of=2026-10-31&format=pdf")
    ev = AuditEvent.objects.get(action="reports.exported")
    assert ev.actor_id == treasurer.pk and ev.changes["format"] == "pdf"
    assert ev.message == "Receivables aging"


def test_pages_show_export_links(books, client, treasurer, today):
    client.force_login(treasurer)
    html = _get(client, "/money/reports/gl/?account=4000&basis=cash").content.decode()
    assert 'href="?account=4000&amp;basis=cash&amp;format=xlsx"' in html
    assert 'data-export="pdf"' in html


def test_unit_list_exports_all_formats(books, client, treasurer):
    client.force_login(treasurer)
    rows = csv_rows(_get(client, "/units/?format=csv").content)
    assert rows[0][0] == "Unit" and [r[0] for r in rows[1:]] == ["A", "B", "C"]
    x = xlsx_rows(_get(client, "/units/?format=xlsx").content)
    assert x[-1][4] == 33.33  # allocated interest as a number
    assert _get(client, "/units/?format=pdf").content.startswith(b"%PDF")
    assert AuditEvent.objects.filter(action="reports.exported").count() == 3
