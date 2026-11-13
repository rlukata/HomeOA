"""Signed output: the original PDF plus one signature certificate page (ReportLab + pypdf)."""

from __future__ import annotations

import io
from xml.sax.saxutils import escape

from django.utils.translation import gettext as _
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from homeoa.letters.pdf import FONT, FONT_BOLD  # registers DejaVu Sans

_h1 = ParagraphStyle("h1", fontName=FONT_BOLD, fontSize=15, leading=19, spaceAfter=8)
_body = ParagraphStyle("b", fontName=FONT, fontSize=9.5, leading=13, spaceAfter=6)
_cell = ParagraphStyle("c", fontName=FONT, fontSize=8.5, leading=11)
_mono = ParagraphStyle("m", parent=_cell, fontName="Courier", fontSize=7.5)


def _p(text, style=_cell):
    return Paragraph(escape(str(text)), style)


def certificate_page(envelope, signers) -> bytes:
    """One-page PDF certificate listing every signature on ``envelope``."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER, leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.8 * inch
    )
    story = [
        _p(_("Signature certificate"), _h1),
        _p(_("Document: %(t)s") % {"t": envelope.title}, _body),
        _p(_("File: %(f)s") % {"f": envelope.source_name or envelope.source_key}, _body),
        _p(_("SHA-256 of the original document:"), _body),
        _p(envelope.source_sha256, _mono),
        Spacer(1, 10),
    ]
    rows = [[_p(_("Signer")), _p(_("Signed (UTC)")), _p(_("IP address / browser"))]]
    for s in signers:
        rows.append(
            [
                Paragraph(f"{escape(s.signed_name)}<br/>{escape(s.email)}", _cell),
                _p(s.acted_at.strftime("%Y-%m-%d %H:%M:%S UTC") if s.acted_at else ""),
                _p(f"{s.ip or ''} / {s.user_agent}"),
            ]
        )
    table = Table(rows, colWidths=[2.2 * inch, 1.6 * inch, 3.1 * inch], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, "#999999"),
                ("BACKGROUND", (0, 0), (-1, 0), "#eeeeee"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story += [
        table,
        Spacer(1, 10),
        _p(
            _(
                "Each signer consented to sign electronically, typed their name and clicked "
                "to sign. Before each signature the original document was checked against "
                "the SHA-256 above. The SHA-256 of this signed file is recorded in the "
                "association's signature history and audit log."
            ),
            _body,
        ),
    ]
    doc.build(story)
    return buf.getvalue()


def signed_pdf(original: bytes, certificate: bytes) -> bytes:
    writer = PdfWriter()
    for src in (original, certificate):
        for page in PdfReader(io.BytesIO(src)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
