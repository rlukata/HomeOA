"""AiProvider: AI that fills in a form from a document.

AI only proposes: what it returns fills a form that a person checks and submits. It never
posts to the ledger, sends a notice, approves anything or writes statutory text. The
default does nothing, and AI features stay hidden; the Anthropic plugin uses Claude.

Each call sends only what its task needs. Callers never pass Address Confidentiality
Program addresses, bank account or routing numbers, unlisted phones or confidential
emails. A document's text is untrusted: the provider gets no tools that change anything,
and the answer is validated against ``schema`` and then by the same form a person fills in.
"""

from __future__ import annotations

import abc

from pydantic import BaseModel


class AiProvider(abc.ABC):
    name: str = ""
    #: False for the default, which has no AI: AI features stay hidden.
    available: bool = True

    @abc.abstractmethod
    def read_document(
        self,
        document: bytes,
        *,
        content_type: str,
        instructions: str,
        schema: type[BaseModel],
    ) -> BaseModel | None:
        """Fill ``schema`` from a PDF or image, following ``instructions``. ``None`` when the
        document can't be read or the provider declines."""
