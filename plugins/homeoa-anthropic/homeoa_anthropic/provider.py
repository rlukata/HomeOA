"""``ClaudeAi``: the ``AiProvider`` this plugin registers.

One request per document: the file, the caller's instructions as the system prompt, and
the answer format, which the API enforces (structured outputs). No tools are offered, so
text inside the document can do nothing but fill the fields. A refusal, an error or a
timeout reads as "could not read": the person fills the form in by hand.
"""

from __future__ import annotations

import base64
import logging

from django.utils.translation import gettext_lazy

from homeoa.providers.ai import AiProvider

from .client import api_key, get_client, uses_fake

logger = logging.getLogger(__name__)
MAX_TOKENS = 16000
# Re-run a declined request on Anthropic's recommended model for the refusal's category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
ASK = "Fill in the answer from this document."


def _block(document: bytes, content_type: str) -> dict:
    data = base64.standard_b64encode(document).decode("ascii")
    kind = "document" if content_type == "application/pdf" else "image"
    return {"type": kind, "source": {"type": "base64", "media_type": content_type, "data": data}}


class ClaudeAi(AiProvider):
    name = gettext_lazy("Claude (Anthropic)")

    def __init__(self, installation=None, client=None):
        self.installation = installation
        self._client = client
        config = getattr(installation, "settings", None) or {}
        self.model = config.get("model") or "claude-opus-5-5"

    @property
    def available(self) -> bool:
        return self._client is not None or uses_fake() or bool(api_key(self.installation))

    @property
    def client(self):
        if self._client is None:
            self._client = get_client(self.installation)
        return self._client

    def read_document(self, document, *, content_type, instructions, schema):
        if self.client is None:
            return None
        extra = {}
        if self.model in FALLBACK_MODELS:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": "medium"},
                system=instructions,
                messages=[
                    {
                        "role": "user",
                        "content": [_block(document, content_type), {"type": "text", "text": ASK}],
                    }
                ],
                output_format=schema,
                **extra,
            )
        except Exception as exc:  # noqa: BLE001 - any API failure means "fill it in by hand"
            logger.warning("Claude could not read a document: %s", type(exc).__name__)
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            logger.info("Claude did not answer: %s", response.stop_reason)
            return None
        return response.parsed_output
