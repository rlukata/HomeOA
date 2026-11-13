"""A stand-in for the Anthropic client in tests, dev and E2E: no network, no cost.

``FakeClaude().beta.messages.parse(...)`` answers like Claude for invoices: it reads the
PDF's text layer and picks out the vendor, invoice number, dates and total by pattern,
and names the vendor from the list in the instructions that appears in the text. Photos
and anything else come back empty. Every call is recorded in ``FakeClaude.calls``.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import io
import re
from typing import Any, ClassVar


def _iso(text: str) -> str:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _find(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def read_invoice(pdf_text: str, instructions: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in pdf_text.splitlines() if ln.strip()]
    known = [m.group(1).strip() for m in re.finditer(r"^- (.+)$", instructions, re.MULTILINE)]
    match = next((v for v in known if v.lower() in pdf_text.lower()), "")
    date = r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4})"
    return {
        "vendor": lines[0] if lines else "",
        "vendor_match": match,
        "invoice_number": _find(
            r"Invoice\s*(?:#|No\.?|number)\s*:?\s*([A-Z0-9][A-Z0-9-]*)", pdf_text
        ),
        "invoice_date": _iso(_find(r"Invoice date\s*:?\s*" + date, pdf_text)),
        "due_date": _iso(_find(r"Due(?: date)?\s*:?\s*" + date, pdf_text)),
        "total": _find(r"Total(?: due)?\s*:?\s*\$?\s*([\d,]+\.\d{2})", pdf_text),
        "lines": [],
        "summary": _find(r"For\s*:?\s*(.+)", pdf_text),
    }


@dataclasses.dataclass
class FakeResponse:
    parsed_output: Any
    stop_reason: str = "end_turn"


class _Messages:
    def __init__(self, owner: FakeClaude):
        self.owner = owner

    def parse(self, **kwargs):
        FakeClaude.calls.append(kwargs)
        if FakeClaude.refuse:
            return FakeResponse(None, "refusal")
        schema = kwargs["output_format"]
        block = kwargs["messages"][0]["content"][0]
        text = ""
        if block["type"] == "document":
            from pypdf import PdfReader

            data = base64.standard_b64decode(block["source"]["data"])
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        answer = read_invoice(text, kwargs.get("system", ""))
        fields = schema.model_fields
        return FakeResponse(schema(**{k: v for k, v in answer.items() if k in fields}))


class _Beta:
    def __init__(self, owner):
        self.messages = _Messages(owner)


class FakeClaude:
    calls: ClassVar[list[dict]] = []
    refuse: ClassVar[bool] = False

    def __init__(self):
        self.beta = _Beta(self)

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.refuse = False
