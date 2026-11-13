"""The text inside document files, for search (DOC-05).

``read(version)`` opens the stored file and saves its text page by page as
``DocumentPage`` rows with a PostgreSQL search vector: the text layer of PDFs, plain text
and CSV, the paragraphs of Word files and the cells of Excel files. PDF pages with no text
layer of their own (scans) and photos go to the ``OcrProvider``; the default reads nothing,
the Tesseract plugin reads them on the server. Nothing is sent outside the server.

A new version is read by a background job after upload; a daily job reads any version
still waiting, and reads scans again once OCR is turned on.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile

from django.contrib.postgres.search import SearchVector
from django.db import transaction

from homeoa.providers import get_provider

from .models import DocumentPage, DocumentVersion, TextStatus

logger = logging.getLogger(__name__)

# The text search configuration (word forms: "assessments" finds "assessment").
SEARCH_CONFIG = "english"
# A PDF page with an image and less text than this is treated as a scan.
MIN_LAYER_CHARS = 20
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEXT_TYPES = ("text/plain", "text/csv")


class _Unreadable(Exception):
    pass


def _has_image(page) -> bool:
    """Whether the page draws an image (or a form that may hold one), as scans do."""
    try:
        xobjects = page["/Resources"].get_object().get("/XObject")
        if xobjects is None:
            return False
        return any(
            x.get_object().get("/Subtype") in ("/Image", "/Form")
            for x in xobjects.get_object().values()
        )
    except Exception:  # noqa: BLE001 - odd resources: treat the page as text only
        return False


def _pdf(data: bytes, ocr) -> tuple[dict[int, tuple[str, bool]], bool]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        layer, images = {}, set()
        for n, page in enumerate(reader.pages, 1):
            layer[n] = page.extract_text() or ""
            if _has_image(page):
                images.add(n)
    except Exception as exc:  # noqa: BLE001 - encrypted or damaged files are reported, not raised
        raise _Unreadable(str(exc)) from exc
    scans = [n for n, text in layer.items() if n in images and len(text.strip()) < MIN_LAYER_CHARS]
    read = ocr.read_pdf_pages(data, scans) if scans and ocr.reads else {}
    pages = {}
    for n, text in layer.items():
        scanned = (read.get(n) or "").strip()
        pages[n] = (scanned, True) if scanned else (text, False)
    unread = any(n in scans and not ocr.reads for n in layer)
    return pages, unread


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise _Unreadable(str(exc)) from exc
    paragraphs = re.split(r"</w:p>", xml)
    return "\n".join("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in paragraphs).strip()


def _xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - a damaged workbook is reported, not raised
        raise _Unreadable(str(exc)) from exc
    lines = []
    for sheet in book.worksheets:
        lines.append(sheet.title)
        for row in sheet.iter_rows(values_only=True):
            cells = [str(v) for v in row if v not in (None, "")]
            if cells:
                lines.append(" ".join(cells))
    return "\n".join(lines)


def _pages(version: DocumentVersion, data: bytes, ocr) -> tuple[dict, bool]:
    """``({page: (text, from_ocr)}, scans_left_unread)`` for the file's type."""
    ctype = version.content_type
    if ctype == PDF:
        return _pdf(data, ocr)
    if ctype.startswith("image/"):
        if not ocr.reads:
            return {1: ("", False)}, True
        return {1: (ocr.read_image(data, content_type=ctype), True)}, False
    if ctype in TEXT_TYPES:
        return {1: (data.decode("utf-8", "replace"), False)}, False
    if ctype == DOCX:
        return {1: (_docx(data), False)}, False
    if ctype == XLSX:
        return {1: (_xlsx(data), False)}, False
    return {}, False


def read(version: DocumentVersion) -> str:
    """Read the version's file into ``DocumentPage`` rows (replacing any earlier ones) and
    return the new ``text_status``. A file that can't be opened is marked failed."""
    from .services import read_file

    ocr = get_provider("OcrProvider", version.association)
    try:
        pages, unread = _pages(version, read_file(version), ocr)
    except _Unreadable as exc:
        logger.warning("Could not read the text of document version %s: %s", version.pk, exc)
        pages, unread, status = {}, False, TextStatus.FAILED
    else:
        if unread:
            status = TextStatus.UNREAD
        elif any(text.strip() for text, _ocr in pages.values()):
            status = TextStatus.READ
        else:
            status = TextStatus.EMPTY
    with transaction.atomic():
        DocumentPage.all_associations.filter(version=version).delete()
        DocumentPage.all_associations.bulk_create(
            DocumentPage(
                association_id=version.association_id,
                version=version,
                number=n,
                text=text.replace("\x00", ""),
                ocr=from_ocr,
            )
            for n, (text, from_ocr) in sorted(pages.items())
        )
        DocumentPage.all_associations.filter(version=version).update(
            search=SearchVector("text", config=SEARCH_CONFIG)
        )
        DocumentVersion.all_associations.filter(pk=version.pk).update(text_status=status)
    version.text_status = status
    return status


def waiting(association, *, limit: int) -> list[DocumentVersion]:
    """Versions to read: never read, and, once OCR is on, those with scans left unread."""
    statuses = [TextStatus.PENDING]
    if get_provider("OcrProvider", association).reads:
        statuses.append(TextStatus.UNREAD)
    return list(
        DocumentVersion.all_associations.filter(
            association=association, text_status__in=statuses
        ).order_by("pk")[:limit]
    )
