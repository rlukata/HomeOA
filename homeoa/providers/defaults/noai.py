"""Default AiProvider: no AI. AI features stay hidden and nothing is sent anywhere."""

from __future__ import annotations

from homeoa.providers.ai import AiProvider


class NoAi(AiProvider):
    name = "none"
    available = False

    def read_document(self, document, *, content_type, instructions, schema):
        return None
