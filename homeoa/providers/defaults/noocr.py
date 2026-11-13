"""Default OcrProvider: reads nothing. Scans and photos are found by title only."""

from __future__ import annotations

from collections.abc import Sequence

from homeoa.providers.ocr import OcrProvider


class NoOcr(OcrProvider):
    name = "none"
    reads = False

    def read_image(self, image: bytes, *, content_type: str) -> str:
        return ""

    def read_pdf_pages(self, pdf: bytes, pages: Sequence[int]) -> dict[int, str]:
        return {}
