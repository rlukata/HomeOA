"""Drive the fake Stripe provider (for E2E). Prints one JSON line.

    stripe_fake last-intent                     -> {id, amount, status}
    stripe_fake fail-next                       -> {fail_next: true}
    stripe_fake webhook <event_type> <intent_id> -> {body, signature, association_id}

Refuses to run with production settings.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from homeoa_stripe import fake
from homeoa_stripe.models import StripeIntent
from homeoa_stripe.services import PLUGIN_ID


class Command(BaseCommand):
    help = "Drive the fake Stripe provider (testing only)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["last-intent", "fail-next", "webhook"])
        parser.add_argument("event_type", nargs="?")
        parser.add_argument("intent_id", nargs="?")

    def handle(self, *args, action, event_type=None, intent_id=None, **options):
        if not (getattr(settings, "HOMEOA_FIXED_TODAY_ALLOWED", False) or settings.DEBUG):
            raise CommandError("stripe_fake is for test environments only.")
        if action == "fail-next":
            fake.fail_next()
            out = {"fail_next": True}
        elif action == "last-intent":
            intent = StripeIntent.all_associations.order_by("-created_at", "-pk").first()
            if intent is None:
                raise CommandError("No intents yet.")
            out = {"id": intent.intent_id, "amount": str(intent.charged), "status": intent.status}
        else:
            if not (event_type and intent_id):
                raise CommandError("webhook needs <event_type> <intent_id>.")
            out = self._webhook(event_type, intent_id)
        self.stdout.write(json.dumps(out))

    def _webhook(self, event_type: str, intent_id: str) -> dict:
        from homeoa.plugins.services import installation_for

        intent = StripeIntent.all_associations.filter(intent_id=intent_id).first()
        if intent is None:
            raise CommandError(f"Unknown intent {intent_id}.")
        inst = installation_for(intent.association, PLUGIN_ID)
        secret = (inst.secrets or {}).get("webhook_secret", "") if inst else ""
        if not secret:
            raise CommandError("Stripe is not set up with a webhook secret.")
        status = {"payment_intent.succeeded": "succeeded"}.get(
            event_type, "requires_payment_method"
        )
        obj = {
            "id": intent.intent_id,
            "object": "payment_intent",
            "amount": fake.to_cents(intent.charged),
            "currency": "usd",
            "status": status,
        }
        if event_type == "payment_intent.payment_failed":
            obj["last_payment_error"] = {"message": "Insufficient funds (fake)."}
        body = fake.event(event_type, obj, fee=fake.to_cents(intent.fee))
        return {
            "body": body.decode(),
            "signature": fake.sign(body, secret),
            "association_id": intent.association_id,
        }
