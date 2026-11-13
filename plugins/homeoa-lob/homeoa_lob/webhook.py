"""Lob tracking webhooks -> mail item and delivery status.

Arrives at ``POST /hooks/lob/<association_id>/``; the core webhook view checks the
signature, processes each event id once, and runs ``handle`` in a transaction.
"""

from __future__ import annotations

import hmac
import json

from homeoa.plugins.manifest import WebhookHandler

from .fake import sign

SIGNATURE_HEADER = "Lob-Signature"
TIMESTAMP_HEADER = "Lob-Signature-Timestamp"

EVENTS = {
    "letter.in_transit": "in_transit",
    "letter.in_local_area": "in_transit",
    "letter.processed_for_delivery": "in_transit",
    "letter.delivered": "delivered",
    "letter.certified.delivered": "delivered",
    "letter.returned_to_sender": "returned",
    "letter.certified.returned_to_sender": "returned",
    "letter.failed": "failed",
}


def _payload(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class LobWebhook(WebhookHandler):
    def verify_signature(self, request, installation) -> bool:
        secret = (installation.secrets or {}).get("webhook_secret", "")
        given = request.headers.get(SIGNATURE_HEADER, "")
        stamp = request.headers.get(TIMESTAMP_HEADER, "")
        if not (secret and given and stamp):
            return False
        return hmac.compare_digest(given, sign(request.body, stamp, secret))

    def event_id(self, request) -> str:
        return str(_payload(request).get("id") or "")

    def handle(self, request, installation) -> None:
        from homeoa.letters.batches import record_tracking_event

        data = _payload(request)
        event_type = (data.get("event_type") or {}).get("id", "")
        kind = EVENTS.get(event_type)
        if kind is None:
            return
        letter_id = str((data.get("body") or {}).get("id") or "")
        record_tracking_event(installation.association, letter_id, kind)
