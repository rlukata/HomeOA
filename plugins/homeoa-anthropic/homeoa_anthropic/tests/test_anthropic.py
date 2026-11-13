"""Claude (Anthropic) plugin: AI assistance with one request per document.
Never calls the real API: the fake client, or the real SDK over a mocked transport."""

import json

import pytest

from homeoa import providers
from homeoa.assist.invoices import INSTRUCTIONS, InvoiceFields
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.documents.tests.test_text import _pdf
from homeoa.plugins import registry
from homeoa.plugins import services as plugins
from homeoa_anthropic import client as client_mod
from homeoa_anthropic.fake import FakeClaude
from homeoa_anthropic.manifest import manifest
from homeoa_anthropic.provider import FALLBACK_BETA, ClaudeAi

INVOICE = _pdf(
    "City of Tukwila Utilities\nInvoice # UT-2026-10\nInvoice date: 2026-10-01\n"
    "Due: 2026-10-25\nFor: Water and sewer\nTotal due: $612.34"
)
VENDORS = INSTRUCTIONS + "- City of Tukwila Utilities\n- Greenway Landscaping"


@pytest.fixture(autouse=True)
def plugin_loaded():
    registry.register(manifest)
    FakeClaude.reset()


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def _read(ai, document=INVOICE, content_type="application/pdf"):
    return ai.read_document(
        document, content_type=content_type, instructions=VENDORS, schema=InvoiceFields
    )


def test_serves_ai_once_turned_on(assoc):
    assert providers.get_provider("AiProvider", assoc).available is False  # the default
    plugins.save_installation(
        assoc, "anthropic", enabled=True, data={"api_key": "", "model": "claude-haiku-5-5"}
    )
    ai = providers.get_provider("AiProvider", assoc)
    assert isinstance(ai, ClaudeAi) and ai.model == "claude-haiku-5-5" and ai.available


def test_one_request_with_the_document_and_the_answer_format():
    answer = _read(ClaudeAi())
    assert answer.vendor_match == "City of Tukwila Utilities"
    assert (answer.invoice_number, answer.invoice_date, answer.due_date, answer.total) == (
        "UT-2026-10",
        "2026-10-01",
        "2026-10-25",
        "612.34",
    )
    (call,) = FakeClaude.calls
    assert call["model"] == "claude-opus-5-5" and call["system"] == VENDORS
    assert call["output_format"] is InvoiceFields and "tools" not in call
    assert call["fallbacks"] == "default" and call["betas"] == [FALLBACK_BETA]
    block = call["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"


def test_photos_go_as_images_and_haiku_has_no_fallback():
    ai = ClaudeAi()
    ai.model = "claude-haiku-5-5"
    _read(ai, b"\x89PNG fake", "image/png")
    (call,) = FakeClaude.calls
    assert call["messages"][0]["content"][0]["type"] == "image"
    assert "fallbacks" not in call


def test_refusals_and_errors_read_as_no_answer():
    FakeClaude.refuse = True
    assert _read(ClaudeAi()) is None

    class Broken:
        class beta:  # noqa: N801
            class messages:  # noqa: N801
                @staticmethod
                def parse(**kwargs):
                    raise TimeoutError

    assert _read(ClaudeAi(client=Broken())) is None


def test_live_without_a_key_is_unavailable(settings):
    settings.HOMEOA_ANTHROPIC_CLIENT = "live"
    ai = ClaudeAi()
    assert ai.available is False and _read(ai) is None


def test_production_refuses_the_fake(settings):
    from django.core.exceptions import ImproperlyConfigured

    settings.DEBUG = False
    settings.HOMEOA_FIXED_TODAY_ALLOWED = False
    with pytest.raises(ImproperlyConfigured):
        client_mod.get_client(None)


def test_real_sdk_request_over_a_mocked_transport():
    """The official SDK accepts the request and parses the structured answer."""
    import anthropic
    import httpx2

    seen = {}
    answer = {
        "vendor": "City of Tukwila Utilities",
        "vendor_match": "City of Tukwila Utilities",
        "invoice_number": "UT-2026-10",
        "invoice_date": "2026-10-01",
        "due_date": "2026-10-25",
        "total": "612.34",
        "lines": [],
        "summary": "Water and sewer",
    }

    def handler(request):
        seen["beta"] = request.headers.get("anthropic-beta", "")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(answer)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1200, "output_tokens": 90},
            },
        )

    sdk = anthropic.Anthropic(
        api_key="sk-ant-test-not-real",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    result = _read(ClaudeAi(client=sdk))
    assert result == InvoiceFields(**answer)
    assert FALLBACK_BETA in seen["beta"]
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][0]["type"] == "document"
