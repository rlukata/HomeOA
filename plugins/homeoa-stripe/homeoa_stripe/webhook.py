"""Stripe webhooks -> ledger.

Arrives at ``POST /hooks/stripe/<association_id>/``; the core webhook view checks the
signature, processes each event id once, and runs ``handle`` in a transaction.
``services.intent_succeeded`` is also idempotent per intent id.

Events: ``payment_intent.succeeded``, ``payment_intent.payment_failed`` and ``charge.failed``
(an ACH return after success voids the receipt), ``payout.paid`` / ``payout.failed``.
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import time

from homeoa import audit
from homeoa.plugins.manifest import WebhookHandler

from . import fake, services

SIGNATURE_HEADER = "Stripe-Signature"
TOLERANCE_SECONDS = 300


def _payload(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse(header: str) -> tuple[str, list[str]]:
    ts, sigs = "", []
    for part in header.split(","):
        key, _sep, value = part.strip().partition("=")
        if key == "t":
            ts = value
        elif key == "v1":
            sigs.append(value)
    return ts, sigs


def _arrival(value, association):
    """Stripe's ``arrival_date`` (unix seconds) as a date in the association's zone."""
    from zoneinfo import ZoneInfo

    from homeoa.core.context import association_today

    if not isinstance(value, int):
        return association_today(association)
    try:
        tz = ZoneInfo(association.timezone)
    except Exception:
        tz = dt.UTC
    return dt.datetime.fromtimestamp(value, tz).date()


def _live_fee(installation, intent_id: str):
    """Real events carry no fee: read it from the balance transaction (one request).
    On any failure fall back to the estimate (None)."""
    from .client import get_client

    try:
        client = get_client(installation)
        reader = getattr(client, "intent_fee", None)
        return reader(intent_id) if reader else None
    except Exception:
        return None


class StripeWebhook(WebhookHandler):
    def verify_signature(self, request, installation) -> bool:
        secret = (installation.secrets or {}).get("webhook_secret", "")
        ts, sigs = _parse(request.headers.get(SIGNATURE_HEADER, ""))
        if not (secret and ts.isdigit() and sigs):
            return False
        if abs(time.time() - int(ts)) > TOLERANCE_SECONDS:
            return False
        expected = fake.sign(request.body, secret, int(ts)).split("v1=", 1)[1]
        return any(hmac.compare_digest(expected, s) for s in sigs)

    def event_id(self, request) -> str:
        return str(_payload(request).get("id") or "")

    def handle(self, request, installation) -> None:
        data = _payload(request)
        obj = (data.get("data") or {}).get("object") or {}
        intent_id = str(obj.get("id") or "")
        association = installation.association
        with audit.audit_context(label=f"webhook:{data.get('id', '')}"):
            match data.get("type"):
                case "payment_intent.succeeded":
                    fee = obj.get("homeoa_fee")
                    if fee is None and intent_id:
                        fee = _live_fee(installation, intent_id)
                    services.intent_succeeded(
                        association,
                        intent_id,
                        fake.from_cents(fee) if isinstance(fee, int) and fee >= 0 else None,
                    )
                case "payment_intent.payment_failed":
                    error = (obj.get("last_payment_error") or {}).get("message", "")
                    services.intent_failed(association, intent_id, str(error))
                case "charge.failed":
                    services.intent_failed(
                        association,
                        str(obj.get("payment_intent") or ""),
                        str(obj.get("failure_message") or ""),
                    )
                case "payout.paid":
                    amount = obj.get("amount")
                    if intent_id and isinstance(amount, int) and amount > 0:
                        services.payout_paid(
                            association,
                            intent_id,
                            fake.from_cents(amount),
                            _arrival(obj.get("arrival_date"), association),
                        )
                case "payout.failed":
                    services.payout_failed(association, intent_id)
