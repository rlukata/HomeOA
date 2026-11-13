"""OcrProvider: reading the text of scanned pages and photos (DOC-05).

The default reads nothing, so scans are found by title only; the Tesseract plugin reads
them on the server, so nothing leaves it. Text extraction calls this only for pages that
have no text layer of their own.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence


class OcrProvider(abc.ABC):
    name: str = ""
    #: False for the default, which reads nothing (scans keep no searchable text).
    reads: bool = True

    @abc.abstractmethod
    def read_image(self, image: bytes, *, content_type: str) -> str:
        """The text in a PNG or JPEG image ("" when there is none)."""

    @abc.abstractmethod
    def read_pdf_pages(self, pdf: bytes, pages: Sequence[int]) -> dict[int, str]:
        """The text of the given pages of a PDF (1-based page numbers), by page number."""
