"""Letter PDFs (US Letter) laid out for a #10 single-window envelope.

Library: ReportLab (BSD, pure Python since 4.0; only Pillow besides). WeasyPrint needs
Pango/Cairo system libraries, which we avoid for a self-hosted single-container install.

Layout, measured from the top-left of the page (inches):

* letterhead: association name and address at the top, 0.5–1.6.
* recipient address box: left 1.0, top 2.25, 3.5 wide, 0.875 tall (5 lines at 10pt).
  Tri-folded, the top panel ends at 3.67; a #10 window (4.5 x 1.125, 0.875 from the left
  and 0.5 from the bottom of the envelope) then shows roughly 2.04–3.17 from the page
  top, so the box sits inside it with room for the letter to slide.
* body starts at 3.5 on page 1 and 1.0 on later pages.

Font: DejaVu Sans (bundled in ``letters/fonts/``, free license in ``fonts/LICENSE``), so
names and addresses outside Latin-1 (e.g. "Nguyễn", "Łukasz") print instead of boxes.
"""

from __future__ import annotations

import io
from pathlib import Path
from xml.sax.saxutils import escape

from django.utils.translation import gettext as _
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.platypus.doctemplate import BaseDocTemplate, NextPageTemplate

PAGE_W, PAGE_H = LETTER
MARGIN_X = 1.0 * inch
ADDRESS_BOX = {"left": 1.0, "top": 2.25, "width": 3.5, "height": 0.875}  # inches
ADDRESS_FONT_SIZE = 10
ADDRESS_LEADING = 12
ADDRESS_MAX_LINES = 5
BODY_TOP_FIRST = 3.5 * inch
BODY_TOP_LATER = 1.0 * inch
BOTTOM_MARGIN = 0.9 * inch

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"


def _register_fonts() -> None:
    registered = pdfmetrics.getRegisteredFontNames()
    for name in (FONT, FONT_BOLD):
        if name not in registered:
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / f"{name}.ttf")))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT_BOLD)


_register_fonts()

_body_style = ParagraphStyle("body", fontName=FONT, fontSize=11, leading=15, spaceAfter=9)
_subject_style = ParagraphStyle("subject", parent=_body_style, fontName=FONT_BOLD)


def _draw_letterhead(c, head: dict):
    top = PAGE_H - 0.5 * inch
    c.setFont(FONT_BOLD, 14)
    c.drawString(MARGIN_X, top - 14, head["name"])
    c.setFont(FONT, 9)
    y = top - 30
    contact = " · ".join(x for x in (head.get("phone"), head.get("email")) if x)
    for line in [*head.get("address_lines", []), contact]:
        if line:
            c.drawString(MARGIN_X, y, line)
            y -= 11
    c.setLineWidth(0.5)
    c.line(MARGIN_X, PAGE_H - 1.6 * inch, PAGE_W - MARGIN_X, PAGE_H - 1.6 * inch)


def _draw_address(c, lines: list[str]):
    box = ADDRESS_BOX
    x = box["left"] * inch
    y = PAGE_H - box["top"] * inch - ADDRESS_FONT_SIZE
    c.setFont(FONT, ADDRESS_FONT_SIZE)
    for line in lines[:ADDRESS_MAX_LINES]:
        c.drawString(x, y, line[:60])
        y -= ADDRESS_LEADING


def _footer(c, doc, note: str):
    c.setFont(FONT, 8)
    c.drawRightString(PAGE_W - MARGIN_X, 0.5 * inch, _("Page %(n)s") % {"n": doc.page})
    if note:
        c.drawString(MARGIN_X, 0.5 * inch, note)


def build_pdf(
    *,
    letterhead: dict,
    address_lines: list[str],
    date_text: str,
    subject: str,
    body: str,
    footer_note: str = "",
    title: str = "",
    tables: list[dict] | None = None,
) -> bytes:
    """Render a letter. ``tables`` are appended after the body, each a dict with
    ``rows`` (header row first, cells are strings), optional ``heading`` and
    ``num_cols`` (column indexes aligned right) and ``bold_last`` (total row)."""
    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=LETTER, title=title or subject, author=letterhead["name"], creator="HomeOA"
    )
    width = PAGE_W - 2 * MARGIN_X
    first = Frame(
        MARGIN_X, BOTTOM_MARGIN, width, PAGE_H - BODY_TOP_FIRST - BOTTOM_MARGIN, id="first"
    )
    later = Frame(
        MARGIN_X, BOTTOM_MARGIN, width, PAGE_H - BODY_TOP_LATER - BOTTOM_MARGIN, id="later"
    )

    def on_first(c, d):
        _draw_letterhead(c, letterhead)
        _draw_address(c, address_lines)
        _footer(c, d, footer_note)

    def on_later(c, d):
        _footer(c, d, footer_note)

    doc.addPageTemplates(
        [
            PageTemplate(id="First", frames=[first], onPage=on_first),
            PageTemplate(id="Later", frames=[later], onPage=on_later),
        ]
    )
    story = [NextPageTemplate("Later"), Paragraph(escape(date_text), _body_style)]
    if subject:
        story += [Spacer(1, 4), Paragraph(escape(subject), _subject_style)]
    for para in _paragraphs(body):
        story.append(Paragraph(para, _body_style))
    for spec in tables or []:
        story += _table(spec, width)
    doc.build(story)
    return buf.getvalue()


def _paragraphs(body: str) -> list[str]:
    """Blank lines split paragraphs; single newlines become line breaks."""
    text = (body or "").replace("\r\n", "\n").strip()
    out = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if block.strip():
            out.append("<br/>".join(escape(line) for line in block.split("\n")))
    return out


def _table(spec: dict, width: float) -> list:
    out = []
    if spec.get("heading"):
        out += [Spacer(1, 6), Paragraph(escape(spec["heading"]), _subject_style)]
    rows = spec["rows"]
    ncols = len(rows[0])
    widths = spec.get("col_widths") or [width / ncols] * ncols
    table = Table([list(r) for r in rows], colWidths=widths, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f4")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
    for col in spec.get("num_cols", ()):
        style.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    if spec.get("bold_last"):
        style += [
            ("FONTNAME", (0, -1), (-1, -1), FONT_BOLD),
            ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black),
        ]
    table.setStyle(TableStyle(style))
    out.append(table)
    return out
