"""Tesseract OCR plugin: reads scans and photos on the server (DOC-05)."""

import io
import shutil
import subprocess

import pytest

from homeoa import providers
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.documents.tests.test_text import _pdf
from homeoa.plugins import registry
from homeoa.plugins import services as plugins
from homeoa_tesseract import provider
from homeoa_tesseract.manifest import manifest
from homeoa_tesseract.provider import TesseractOcr


@pytest.fixture(autouse=True)
def plugin_loaded():
    registry.register(manifest)


@pytest.fixture
def ran(monkeypatch):
    """Fakes the tesseract program: records each call and answers "hello"."""
    calls = []

    def run(args, *, input, capture_output, timeout, check):
        calls.append((args, input))
        return subprocess.CompletedProcess(args, 0, stdout=b"hello\n", stderr=b"")

    monkeypatch.setattr(provider.subprocess, "run", run)
    return calls


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def test_serves_ocr_once_turned_on_for_the_association(assoc):
    assert providers.get_provider("OcrProvider", assoc).reads is False  # the default
    plugins.save_installation(assoc, "tesseract", enabled=True, data={"language": "eng+spa"})
    ocr = providers.get_provider("OcrProvider", assoc)
    assert isinstance(ocr, TesseractOcr) and ocr.language == "eng+spa"


def test_reads_nothing_without_the_program(settings):
    settings.HOMEOA_TESSERACT_CMD = "/nonexistent/tesseract"
    assert TesseractOcr().reads is False


def test_photos_and_drawn_pdf_pages_go_to_tesseract(ran):
    ocr = TesseractOcr()
    assert ocr.read_image(b"\x89PNG fake", content_type="image/png") == "hello"
    assert ran[0][0][-2:] == ["-l", "eng"] and ran[0][1] == b"\x89PNG fake"
    assert ocr.read_pdf_pages(_pdf("", ""), [2, 9]) == {2: "hello"}  # page 9 doesn't exist
    assert ran[1][1].startswith(b"\x89PNG")  # the page was drawn as an image


def test_a_failing_run_reads_as_no_text(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("tesseract", 120)

    monkeypatch.setattr(provider.subprocess, "run", fail)
    assert TesseractOcr().read_image(b"x", content_type="image/png") == ""


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_reads_a_real_image():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("L", (900, 200), 255)
    ImageDraw.Draw(image).text((40, 60), "ROOF REPAIR", fill=0, font=ImageFont.load_default(72))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    assert "ROOF" in TesseractOcr().read_image(buf.getvalue(), content_type="image/png")
