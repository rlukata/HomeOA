"""Bounce tracking (COM-02) through the plugin webhook framework.

Core ships a built-in plugin manifest, ``email_bounces``, so bounces arrive at
``POST /hooks/email_bounces/<association_id>/`` like any plugin webhook (signature check,
one-time processing per event id, transaction). The board enables it on
``/settings/plugins/`` and sets a shared secret.

Default payload (the SMTP default has no provider-specific format; a relay or a script that
reads the bounce mailbox posts this JSON)::

    {"id": "<unique event id>", "type": "bounce",
     "message_id": "<Message-ID of the sent email>", "reason": "550 mailbox unavailable"}

signed with ``X-HomeOA-Signature: sha256=<hex HMAC-SHA256 of the raw body with the secret>``.
An email provider plugin with its own format ships its own webhook handler and calls
``mark_bounced``.

Resend format (``format = "resend"``): Resend signs webhooks with Svix. The secret
is the ``whsec_...`` signing secret; headers ``svix-id``, ``svix-timestamp`` and
``svix-signature`` (space-separated ``v1,<base64>`` entries) sign
``"{id}.{timestamp}.{body}"`` with HMAC-SHA256 under the base64-decoded secret. A timestamp
more than 5 minutes off is rejected; ``svix-id`` is the event id (one-time processing).
Only ``email.bounced`` events act; other types are acknowledged and ignored.

Matching a Resend bounce to a delivery: Django's SMTP backend does not expose the id that
Resend's SMTP relay assigns, so deliveries store our own ``Message-ID``. The handler first
tries ``data.email_id`` (matches if a provider plugin stored Resend's id), then the
``Message-ID`` found in ``data.headers`` (list of ``{name, value}`` or a mapping), which
Resend echoes for SMTP-sent mail.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
import time
from typing import Literal

import pydantic
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import PluginManifest, WebhookHandler

logger = logging.getLogger(__name__)

PLUGIN_ID = "email_bounces"
SIGNATURE_HEADER = "X-HomeOA-Signature"
SVIX_TOLERANCE_SECONDS = 5 * 60


class BounceSettings(pydantic.BaseModel):
    secret: str = pydantic.Field(min_length=16, description="Shared webhook secret")
    format: Literal["default", "resend"] = pydantic.Field(
        "default", description="Payload format: default JSON, or Resend (Svix-signed)"
    )


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def mark_bounced(association, message_id: str, reason: str = ""):
    """Mark the email delivery with this id bounced. Returns it, or None.

    Matches the relay's provider id first (e.g. Resend's ``email_id`` from the SMTP
    DATA reply), then our own Message-ID (``Delivery.message_id``; older deliveries
    carry it in ``provider_id``).
    """
    from .models import Delivery

    if not message_id:
        return None
    email = Delivery.all_associations.filter(association=association, channel="email")
    delivery = (
        email.filter(provider_id=message_id).first() or email.filter(message_id=message_id).first()
    )
    if delivery is None:
        logger.info("bounce for unknown message %r ignored", message_id)
        return None
    if delivery.status == Delivery.Status.BOUNCED:
        return delivery
    delivery.status = Delivery.Status.BOUNCED
    delivery.bounced_at = timezone.now()
    delivery.resolved_at = None
    delivery.error = (reason or "bounced")[:2000]
    delivery.save(update_fields=["status", "bounced_at", "resolved_at", "error"])
    from .consent import after_email_outcome

    after_email_outcome(delivery)
    return delivery


def _svix_key(secret: str) -> bytes | None:
    try:
        return base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except binascii.Error, ValueError:
        return None


def svix_sign(body: bytes, secret: str, msg_id: str, timestamp: int | str) -> str:
    """The ``svix-signature`` header value for this body (for tests and tooling)."""
    key = _svix_key(secret) or b""
    signed = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def svix_verify(headers, body: bytes, secret: str, now: float | None = None) -> bool:
    msg_id = headers.get("svix-id", "")
    stamp = headers.get("svix-timestamp", "")
    given = headers.get("svix-signature", "")
    if not (secret and msg_id and stamp and given) or _svix_key(secret) is None:
        return False
    try:
        ts = int(stamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > SVIX_TOLERANCE_SECONDS:
        return False
    expected = svix_sign(body, secret, msg_id, stamp).encode()
    found = False
    for entry in given.split():
        if entry.startswith("v1,"):
            found |= hmac.compare_digest(entry.encode(), expected)
    return found


def _payload(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_resend(installation) -> bool:
    return (installation.settings or {}).get("format") == "resend"


def _header_value(headers, name: str) -> str:
    name = name.lower()
    if isinstance(headers, dict):
        items = headers.items()
    elif isinstance(headers, list):
        items = [(h.get("name"), h.get("value")) for h in headers if isinstance(h, dict)]
    else:
        return ""
    for key, value in items:
        if str(key or "").lower() == name:
            return str(value or "")
    return ""


def handle_resend(association, data: dict):
    """Act on one Resend webhook event. Returns the bounced delivery, or None."""
    if data.get("type") != "email.bounced":
        return None
    event = data.get("data") or {}
    if not isinstance(event, dict):
        return None
    bounce = event.get("bounce") or {}
    reason = str((bounce.get("message") if isinstance(bounce, dict) else "") or "bounced")
    email_id = str(event.get("email_id") or "")
    delivery = mark_bounced(association, email_id, reason) if email_id else None
    if delivery is None:
        message_id = _header_value(event.get("headers"), "Message-ID")
        if message_id:
            delivery = mark_bounced(association, message_id, reason)
    return delivery


class DefaultBounceWebhook(WebhookHandler):
    """Both formats; the installation's ``format`` setting picks one."""

    def verify_signature(self, request, installation) -> bool:
        secret = (installation.secrets or {}).get("secret", "")
        if _is_resend(installation):
            return svix_verify(request.headers, request.body, secret)
        given = request.headers.get(SIGNATURE_HEADER, "")
        return bool(secret) and hmac.compare_digest(given, sign(request.body, secret))

    def _payload(self, request) -> dict:
        return _payload(request)

    def event_id(self, request) -> str:
        svix_id = request.headers.get("svix-id", "")
        if svix_id:
            return svix_id
        return str(self._payload(request).get("id") or "")

    def handle(self, request, installation) -> None:
        data = self._payload(request)
        if _is_resend(installation):
            handle_resend(installation.association, data)
            return
        if data.get("type", "bounce") != "bounce":
            return
        mark_bounced(
            installation.association,
            str(data.get("message_id") or ""),
            str(data.get("reason") or ""),
        )


manifest = PluginManifest(
    id=PLUGIN_ID,
    name=_("Email bounces"),
    version="1.0",
    settings_model=BounceSettings,
    secret_fields=("secret",),
    webhook="homeoa.notify.bounces.DefaultBounceWebhook",
    description=_(
        "Receives bounce reports for sent email and marks the delivery bounced "
        "(built in; default JSON format)."
    ),
)


def ensure_enabled(association):
    """Enable the built-in with a random secret if needed (dev/E2E only). Returns it."""
    from homeoa.plugins import services

    inst = services.installation_for(association, PLUGIN_ID)
    if inst is not None and inst.enabled and (inst.secrets or {}).get("secret"):
        return inst
    return services.save_installation(
        association, PLUGIN_ID, enabled=True, data={"secret": pysecrets.token_hex(24)}
    )


def payload_for(delivery, event_id: str = "", reason: str = "550 5.1.1 mailbox unavailable"):
    return json.dumps(
        {
            "id": event_id or f"bounce-{delivery.pk}-{pysecrets.token_hex(4)}",
            "type": "bounce",
            "message_id": delivery.provider_id,
            "reason": reason,
        }
    ).encode()
