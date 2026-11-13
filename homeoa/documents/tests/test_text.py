"""The text inside document files, read for search (DOC-05)."""

import datetime as dt
import io
import zipfile

import pytest
from django.contrib.auth import get_user_model

from homeoa.documents import services, tasks, text
from homeoa.documents.models import DocumentPage, DocumentVersion, TextStatus, Visibility
from homeoa.documents.tests.test_library import _files, _owner, assoc, board  # noqa: F401
from homeoa.jobs.testing import capture_jobs
from homeoa.providers.ocr import OcrProvider

User = get_user_model()


def _pdf(*pages: str) -> bytes:
    """A PDF with one text page per argument; an empty string makes a page with a picture
    and no text layer, like a scan."""
    from PIL import Image
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    for body in pages:
        if body:
            y = 700
            for line in body.splitlines():
                c.drawString(72, y, line)
                y -= 16
        else:
            c.drawImage(ImageReader(Image.new("L", (60, 80), 200)), 72, 300, 300, 400)
        c.showPage()
    c.save()
    return buf.getvalue()


def _docx(*paragraphs: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>",
        )
    return buf.getvalue()


class FakeOcr(OcrProvider):
    """Reads every scanned page and photo as the given text."""

    name = "fake"

    def __init__(self, said="roof replacement schedule"):
        self.said = said
        self.calls = []

    def read_image(self, image, *, content_type):
        self.calls.append(("image", content_type))
        return self.said

    def read_pdf_pages(self, pdf, pages):
        self.calls.append(("pdf", list(pages)))
        return {n: self.said for n in pages}


@pytest.fixture
def ocr(monkeypatch):
    fake = FakeOcr()
    monkeypatch.setattr(text, "get_provider", lambda kind, association=None: fake)
    return fake


def _upload(assoc, filename, content, visibility=Visibility.OWNERS):  # noqa: F811
    with capture_jobs() as queue:
        version = services.attach(
            assoc, filename=filename, content=content, title=filename, visibility=visibility
        )
        assert "documents.read_text" in queue.pending_names()
        queue.run_all()
    version.refresh_from_db()
    return version


def test_text_of_a_pdf_is_read_page_by_page_after_upload(assoc, board):  # noqa: F811
    v = _upload(
        assoc,
        "declaration.pdf",
        _pdf("Article 5. The board may levy special assessments.", "Pets: two per unit."),
    )
    assert v.text_status == TextStatus.READ
    pages = list(v.pages.order_by("number"))
    assert [p.number for p in pages] == [1, 2]
    assert "special assessments" in pages[0].text and not pages[0].ocr
    # Word forms match: "assessment" finds "assessments"; quoted phrases work.
    for q in ("assessment", '"levy special"', "pets"):
        assert [d.title for d in services.visible_documents(board, assoc, query=q)] == [
            "declaration.pdf"
        ]
    assert not services.visible_documents(board, assoc, query="parking")
    docs = list(services.visible_documents(board, assoc, query="pets"))
    page, snippet = services.text_snippets(docs, "pets")[docs[0].pk]
    assert page == 2 and "<mark>Pets</mark>" in snippet


def test_scanned_pages_wait_for_ocr(assoc, board):  # noqa: F811
    v = _upload(assoc, "minutes-1998.pdf", _pdf("", "Typed page about the roof."))
    assert v.text_status == TextStatus.UNREAD  # no OCR by default: the scan stays unread
    assert v.pages.get(number=1).text == ""
    assert services.visible_documents(board, assoc, query="roof")  # the typed page
    assert not services.visible_documents(board, assoc, query="replacement")


def test_scanned_pages_and_photos_read_by_ocr(assoc, board, ocr):  # noqa: F811
    v = _upload(assoc, "minutes-1998.pdf", _pdf("", "Typed page about the roof."))
    assert ocr.calls == [("pdf", [1])]  # only the page with no text layer
    assert v.text_status == TextStatus.READ
    assert v.pages.get(number=1).ocr and not v.pages.get(number=2).ocr
    assert services.visible_documents(board, assoc, query="replacement schedule")
    photo = _upload(assoc, "notice.png", b"\x89PNG\r\n\x1a\n fake")
    assert photo.text_status == TextStatus.READ and photo.pages.get().ocr


def test_photo_without_ocr_is_unread(assoc):  # noqa: F811
    assert _upload(assoc, "notice.jpg", b"\xff\xd8\xff fake").text_status == TextStatus.UNREAD


def test_text_csv_word_and_excel_files(assoc, board):  # noqa: F811
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Component", "Useful life"])
    wb.active.append(["Gutters", 25])
    xlsx = io.BytesIO()
    wb.save(xlsx)
    for name, content, word in (
        ("rules.txt", b"Quiet hours start at 10 pm.", "quiet"),
        ("roster.csv", b"unit,parking\nA,stall 4\n", "stall"),
        ("policy.docx", _docx("Fence height policy", "No fences over six feet."), "fences"),
        ("reserve.xlsx", xlsx.getvalue(), "gutters"),
    ):
        v = _upload(assoc, name, content)
        assert v.text_status == TextStatus.READ, name
        assert [d.title for d in services.visible_documents(board, assoc, query=word)] == [name]


def test_damaged_file_is_marked_failed(assoc):  # noqa: F811
    assert _upload(assoc, "broken.pdf", b"%PDF-1.4 test\n%%EOF\n").text_status in (
        TextStatus.FAILED,
        TextStatus.EMPTY,
    )
    assert _upload(assoc, "broken.docx", b"not a zip").text_status == TextStatus.FAILED


def test_owners_never_find_board_only_documents_by_their_text(assoc, board):  # noqa: F811
    owner, _unit = _owner()
    _upload(assoc, "legal-advice.pdf", _pdf("Collection strategy for unit A"), Visibility.BOARD)
    assert services.visible_documents(board, assoc, query="collection strategy")
    assert not services.visible_documents(owner, assoc, query="collection strategy")


def test_daily_job_reads_waiting_documents_and_scans_once_ocr_is_on(
    assoc,  # noqa: F811
    monkeypatch,
):
    v = services.attach(assoc, filename="old.pdf", content=_pdf("", "Budget 2019"), title="old")
    assert v.text_status == TextStatus.PENDING  # the upload job has not run
    tasks.read_waiting_text(assoc, dt.date(2026, 10, 16))
    v.refresh_from_db()
    assert v.text_status == TextStatus.UNREAD
    assert text.waiting(assoc, limit=10) == []  # nothing to do while OCR is off
    fake = FakeOcr("water heater")
    monkeypatch.setattr(text, "get_provider", lambda kind, association=None: fake)
    assert text.waiting(assoc, limit=10) == [v]
    tasks.read_waiting_text(assoc, dt.date(2026, 10, 17))
    v.refresh_from_db()
    assert v.text_status == TextStatus.READ
    assert DocumentPage.objects.filter(version=v, text="water heater").exists()


def test_library_search_shows_the_matching_page(client, assoc, board):  # noqa: F811
    _upload(assoc, "bylaws.pdf", _pdf("Section 3. Quorum is a majority of units."))
    client.force_login(board)
    html = client.get("/documents/", {"q": "quorum"}).content.decode()
    assert "bylaws.pdf" in html
    assert 'data-testid="doc-snippet"' in html and "<mark>Quorum</mark>" in html


def test_version_text_status_defaults_to_pending(assoc):  # noqa: F811
    v = services.attach(assoc, filename="a.txt", content=b"hello world", title="a")
    assert DocumentVersion.objects.get(pk=v.pk).text_status == TextStatus.PENDING
