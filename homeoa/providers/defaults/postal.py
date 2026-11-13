"""Default PostalProvider: letters go into a print-ready batch (COM-04).

``send_letter`` only queues a letter for printing; the board prints the batch's combined
PDF, mails it by hand and confirms (``homeoa.letters.batches.confirm_mailed``).
``build_print_batch`` makes the batch files: one combined PDF (each letter starts on a
new sheet, padded to an even page count for duplex printing) and a CSV manifest.
"""

from __future__ import annotations

import csv
import dataclasses
import io

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from ..postal import LetterRequest, LetterResult, LetterStatus, PostalProvider

MANIFEST_COLUMNS = (
    "seq",
    "description",
    "name",
    "line1",
    "line2",
    "city",
    "region",
    "postal_code",
    "country",
    "certified",
    "pages",
)


@dataclasses.dataclass(frozen=True)
class PrintBatch:
    pdf: bytes
    manifest_csv: str
    pages: int


def build_print_batch(letters: list[LetterRequest], *, duplex: bool = True) -> PrintBatch:
    """Combine the letters into one PDF plus a CSV manifest, in the given order."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    out = io.StringIO()
    rows = csv.writer(out)
    rows.writerow(MANIFEST_COLUMNS)
    for seq, letter in enumerate(letters, start=1):
        reader = PdfReader(io.BytesIO(letter.pdf))
        count = len(reader.pages)
        for page in reader.pages:
            writer.add_page(page)
        if duplex and count % 2:
            last = reader.pages[-1]
            writer.add_blank_page(width=last.mediabox.width, height=last.mediabox.height)
        to = letter.to
        rows.writerow(
            [
                seq,
                letter.description,
                to.name,
                to.line1,
                to.line2,
                to.city,
                to.region,
                to.postal_code,
                to.country,
                "yes" if letter.certified else "no",
                count,
            ]
        )
    buf = io.BytesIO()
    if letters:
        writer.write(buf)
    return PrintBatch(pdf=buf.getvalue(), manifest_csv=out.getvalue(), pages=len(writer.pages))


class PrintBatchPostal(PostalProvider):
    prints_locally = True
    name = gettext_lazy("Print and mail by hand")

    def send_letter(self, letter: LetterRequest) -> LetterResult:
        return LetterResult(
            status=LetterStatus.QUEUED_FOR_PRINT,
            message=_("Queued for printing. Print and mail it yourself."),
        )

    def build_batch(self, letters: list[LetterRequest]) -> PrintBatch:
        return build_print_batch(letters)
