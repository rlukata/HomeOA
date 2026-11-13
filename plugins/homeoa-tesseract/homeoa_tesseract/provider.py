"""``TesseractOcr``: the ``OcrProvider`` this plugin registers.

Runs the ``tesseract`` program on the server (``HOMEOA_TESSERACT_CMD``, default
``tesseract``); scanned PDF pages are first drawn as grayscale images with pdfium. When the
program is missing, ``reads`` is false and scans stay unread, as with no OCR at all.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
from collections.abc import Sequence

from django.conf import settings
from django.utils.translation import gettext_lazy

from homeoa.providers.ocr import OcrProvider

logger = logging.getLogger(__name__)
DPI = 300
TIMEOUT_SECONDS = 120


def command() -> str:
    return getattr(settings, "HOMEOA_TESSERACT_CMD", "") or "tesseract"


class TesseractOcr(OcrProvider):
    name = gettext_lazy("Tesseract OCR")

    def __init__(self, installation=None):
        self.installation = installation
        config = getattr(installation, "settings", None) or {}
        self.language = config.get("language") or "eng"

    @property
    def reads(self) -> bool:
        return shutil.which(command()) is not None

    def _run(self, image: bytes) -> str:
        try:
            done = subprocess.run(  # noqa: S603 - fixed program, no shell, image on stdin
                [command(), "stdin", "stdout", "-l", self.language],
                input=image,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("tesseract failed: %s", exc)
            return ""
        return done.stdout.decode("utf-8", "replace").strip()

    def read_image(self, image: bytes, *, content_type: str) -> str:
        return self._run(image)

    def read_pdf_pages(self, pdf: bytes, pages: Sequence[int]) -> dict[int, str]:
        import pypdfium2 as pdfium

        out: dict[int, str] = {}
        doc = pdfium.PdfDocument(pdf)
        try:
            for number in pages:
                if not 1 <= number <= len(doc):
                    continue
                bitmap = doc[number - 1].render(scale=DPI / 72, grayscale=True)
                buf = io.BytesIO()
                bitmap.to_pil().save(buf, format="PNG")
                out[number] = self._run(buf.getvalue())
        finally:
            doc.close()
        return out
