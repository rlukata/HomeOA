"""Export framework (RPT-01): one table spec in, a CSV, XLSX or PDF file out.

A view builds a :class:`TableSpec` (columns with a kind, rows, optional totals) and calls
:func:`render` (bytes) or :func:`homeoa.ui.export.export_response` (HTTP download). The
three formats read the same cells, so totals are identical across them.

* CSV: text cells, ``Decimal`` keeps its exact digits, dates ISO, formula-injection guard.
* XLSX: money and numbers are numeric cells (``Decimal`` converted at 2 places for money),
  dates are date cells; text uses the same injection guard.
* PDF: landscape letter with the bundled DejaVu Sans (Unicode) font from ``letters``.

Adding a report (for example the financial statements) is a ``TableSpec`` builder plus an
``?format=`` check in its view; nothing here changes.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils.translation import gettext as _

CSV = "csv"
XLSX = "xlsx"
PDF = "pdf"
FORMATS = (CSV, XLSX, PDF)
CONTENT_TYPES = {
    CSV: "text/csv; charset=utf-8",
    XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    PDF: "application/pdf",
}

TEXT = "text"
MONEY = "money"
NUMBER = "number"
DATE = "date"
_NUMERIC = (MONEY, NUMBER)

_FORMULA = ("=", "+", "-", "@", "\t", "\r")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Column:
    label: str
    kind: str = TEXT


@dataclass
class TableSpec:
    """A titled table. ``rows`` and ``totals`` hold raw values (Decimal, date, str, None)."""

    title: str
    columns: Sequence[Column]
    rows: Sequence[Sequence] = field(default_factory=list)
    totals: Sequence | None = None  # one row, same width; label in the first text cell
    subtitle: str = ""
    filename: str = ""  # stem, e.g. "trial-balance"; defaults to the title
    pii: bool = False  # exports are audited when the table carries personal data
    watermark: str = ""  # e.g. "contains confidential data": on every format
    intro: Sequence[str] = ()  # PDF only: short paragraphs between the title and the table


def safe_name(text: str) -> str:
    return _SAFE_NAME.sub("-", str(text)).strip("-")


def filename(spec: TableSpec, fmt: str, *, association=None, on: dt.date | None = None) -> str:
    """``<association>-<stem>-<YYYY-MM-DD>.<ext>`` (association and date when given)."""
    parts = []
    if association is not None:
        parts.append(safe_name(association.short_name or association.name))
    parts.append(safe_name(spec.filename or spec.title))
    if on is not None:
        parts.append(on.isoformat())
    stem = "-".join(p for p in parts if p) or "export"
    return f"{stem}.{fmt}"


def text_cell(value) -> str:
    """CSV/PDF text of one value (the shared CSV rules)."""
    if value is None:
        return ""
    if isinstance(value, Decimal | int):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    text = str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def _all_rows(spec: TableSpec):
    yield from spec.rows
    if spec.totals is not None:
        yield spec.totals


def to_csv(spec: TableSpec) -> str:
    """CSV text with a leading BOM so spreadsheet apps read UTF-8."""
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    if spec.watermark:
        writer.writerow([text_cell(str(spec.watermark))])
    writer.writerow([str(c.label) for c in spec.columns])
    for row in _all_rows(spec):
        writer.writerow([text_cell(v) for v in row])
    return buf.getvalue()


def _xlsx_value(value, kind: str):
    if value is None or value == "":
        return None
    if kind in _NUMERIC and isinstance(value, Decimal | int):
        # openpyxl stores numbers as floats in the file format; money is exact to the cent.
        return float(value) if isinstance(value, Decimal) else value
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        from django.utils import timezone

        return timezone.localtime(value).replace(tzinfo=None)  # XLSX has no time zones
    if isinstance(value, dt.datetime | dt.date):
        return value
    if isinstance(value, Decimal | int):
        return float(value) if isinstance(value, Decimal) else value
    return text_cell(value)


def to_xlsx(spec: TableSpec) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = (safe_name(spec.filename or spec.title) or "Export")[:31]
    bold = Font(bold=True)
    start = 1
    title = f"{spec.title} · {spec.watermark}" if spec.watermark else str(spec.title)
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
    if spec.subtitle:
        ws.cell(row=2, column=1, value=str(spec.subtitle))
    start = 4 if spec.subtitle else 3
    for j, col in enumerate(spec.columns, 1):
        ws.cell(row=start, column=j, value=str(col.label)).font = bold
    r = start
    for row in spec.rows:
        r += 1
        _xlsx_row(ws, r, spec.columns, row)
    if spec.totals is not None:
        r += 1
        for c in _xlsx_row(ws, r, spec.columns, spec.totals):
            c.font = bold
    for j, col in enumerate(spec.columns, 1):
        letter = ws.cell(row=start, column=j).column_letter
        ws.column_dimensions[letter].width = 14 if col.kind != TEXT else 28
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_row(ws, r, columns, row):
    cells = []
    for j, (col, value) in enumerate(zip(columns, row, strict=False), 1):
        c = ws.cell(row=r, column=j, value=_xlsx_value(value, col.kind))
        if col.kind == MONEY:
            c.number_format = "#,##0.00;-#,##0.00"
        elif isinstance(value, dt.datetime):
            c.number_format = "yyyy-mm-dd hh:mm"
        elif col.kind == DATE or isinstance(value, dt.date):
            c.number_format = "yyyy-mm-dd"
        cells.append(c)
    return cells


def _pdf_text(value, kind: str) -> str:
    if value is None or value == "":
        return ""
    if kind == MONEY and isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return str(value)


def to_pdf(spec: TableSpec, *, association=None, generated_on: dt.date | None = None) -> bytes:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from homeoa.letters.pdf import FONT, FONT_BOLD  # registers the bundled Unicode font

    cell_style = ParagraphStyle("cell", fontName=FONT, fontSize=8, leading=10)
    head_style = ParagraphStyle("head", parent=cell_style, fontName=FONT_BOLD)
    title_style = ParagraphStyle("title", fontName=FONT_BOLD, fontSize=14, leading=18)
    sub_style = ParagraphStyle("sub", fontName=FONT, fontSize=9, leading=12)
    intro_style = ParagraphStyle("intro", fontName=FONT, fontSize=10, leading=14, spaceBefore=3)

    def para(text, style, kind=TEXT):
        if kind in _NUMERIC:
            style = ParagraphStyle(style.name + "r", parent=style, alignment=2)
        return Paragraph(escape(text), style)

    data = [[para(str(c.label), head_style, c.kind) for c in spec.columns]]
    for row in spec.rows:
        data.append(
            [
                para(_pdf_text(v, c.kind), cell_style, c.kind)
                for c, v in zip(spec.columns, row, strict=False)
            ]
        )
    if spec.totals is not None:
        data.append(
            [
                para(_pdf_text(v, c.kind), head_style, c.kind)
                for c, v in zip(spec.columns, spec.totals, strict=False)
            ]
        )
    table = Table(data, repeatRows=1)
    style = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    if spec.totals is not None:
        style.append(("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black))
    table.setStyle(TableStyle(style))

    head = [para(str(spec.title), title_style)]
    meta = [str(x) for x in (getattr(association, "name", ""), spec.subtitle) if x]
    if generated_on is not None:
        meta.append(_("Generated %(date)s") % {"date": generated_on.isoformat()})
    if meta:
        head.append(para(" · ".join(meta), sub_style))
    if spec.intro:
        head.append(Spacer(1, 0.1 * inch))
        head.extend(para(str(text), intro_style) for text in spec.intro)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(LETTER),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        title=str(spec.title),
    )

    def _watermark(canvas, _doc):
        if not spec.watermark:
            return
        canvas.saveState()
        canvas.setFont(FONT_BOLD, 28)
        canvas.setFillColor(colors.Color(0.8, 0.1, 0.1, alpha=0.18))
        width, height = landscape(LETTER)
        canvas.translate(width / 2, height / 2)
        canvas.rotate(30)
        canvas.drawCentredString(0, 0, str(spec.watermark))
        canvas.restoreState()

    doc.build(
        [*head, Spacer(1, 0.15 * inch), table], onFirstPage=_watermark, onLaterPages=_watermark
    )
    return buf.getvalue()


def render(spec: TableSpec, fmt: str, *, association=None, on: dt.date | None = None) -> bytes:
    if fmt == CSV:
        return to_csv(spec).encode("utf-8")
    if fmt == XLSX:
        return to_xlsx(spec)
    if fmt == PDF:
        return to_pdf(spec, association=association, generated_on=on)
    raise ValueError(f"unknown export format {fmt!r}")
