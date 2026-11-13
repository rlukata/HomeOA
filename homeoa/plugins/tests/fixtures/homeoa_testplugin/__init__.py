"""A tiny plugin used by the plugin-system tests (registered through a fake entry point)."""

import dataclasses
import hashlib
import hmac
import json

from pydantic import BaseModel, Field

from homeoa.events import Event
from homeoa.plugins.manifest import EventHandler, PluginManifest, WebhookHandler

received: list = []
handled: list = []


@dataclasses.dataclass(frozen=True, kw_only=True)
class TestPing(Event):
    name = "test.ping"


def on_ping(event):
    received.append(event)


class Settings(BaseModel):
    account: str = Field(min_length=3)
    limit: int = 10
    api_key: str


class Webhook(WebhookHandler):
    def verify_signature(self, request, installation):
        expected = hmac.new(
            installation.secrets["api_key"].encode(), request.body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, request.headers.get("X-Signature", ""))

    def event_id(self, request):
        return json.loads(request.body)["id"]

    def handle(self, request, installation):
        handled.append(json.loads(request.body)["id"])
        from homeoa import audit

        audit.record("testplugin.webhook", association=installation.association)


manifest = PluginManifest(
    id="testplugin",
    name="Test plugin",
    version="1.0",
    core_requires=">=0.1",
    settings_model=Settings,
    secret_fields=("api_key",),
    providers={"TestProvider": "homeoa.plugins.tests.fixtures.homeoa_testplugin.Webhook"},
    event_handlers=(
        EventHandler("test.ping", "homeoa.plugins.tests.fixtures.homeoa_testplugin.on_ping"),
    ),
    webhook="homeoa.plugins.tests.fixtures.homeoa_testplugin.Webhook",
)

incompatible = dataclasses.replace(manifest, core_requires=">=99")
