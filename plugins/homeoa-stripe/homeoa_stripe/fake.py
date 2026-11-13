"""In-repo fake of the Stripe APIs HomeOA uses (customers, SetupIntents for US bank
accounts, ACH PaymentIntents). No network, no keys.

Cross-process state lives in a small JSON file (``settings.HOMEOA_STRIPE_FAKE_STATE``,
default ``.work/stripe-fake.json``): "fail the next intent" (so ``manage.py stripe_fake
fail-next`` can steer a running server) and the SetupIntents in flight.

Bank verification, like Stripe test mode:

* ``collect_bank_account(setup_id, "instant")`` stands in for Stripe.js Financial
  Connections: confirming then succeeds at once.
* ``collect_bank_account(setup_id, "manual")`` stands in for entering account numbers by
  hand: confirming leaves the SetupIntent in ``requires_action`` until
  ``verify_microdeposits`` gets Stripe's test amounts 32 and 45 (or descriptor code
  ``SM11AA``).
* ``collect_bank_account(setup_id, "fail")`` is a bank that can't be linked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

CENT = Decimal("0.01")
#: Stripe's published US ACH Direct Debit price, used only by the fake: 0.8%, capped at $5.
FEE_RATE = Decimal("0.008")
FEE_CAP = Decimal("5.00")


class FakeStripeError(Exception):
    pass


def ach_fee(amount: Decimal) -> Decimal:
    return min((amount * FEE_RATE).quantize(CENT, rounding=ROUND_HALF_UP), FEE_CAP)


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def from_cents(cents: int) -> Decimal:
    return (Decimal(int(cents)) / 100).quantize(CENT)


def _state_path() -> Path:
    from django.conf import settings

    configured = getattr(settings, "HOMEOA_STRIPE_FAKE_STATE", "")
    return Path(configured) if configured else Path(settings.BASE_DIR) / ".work/stripe-fake.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except OSError, ValueError:
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def fail_next() -> None:
    _write_state({**_read_state(), "fail_next": True})


def reset() -> None:
    _write_state({})


TEST_AMOUNTS = [32, 45]
TEST_DESCRIPTOR_CODE = "SM11AA"
FAKE_LAST4 = "6789"
FAKE_BANK = "StripeBank (fake)"


def _token(prefix: str) -> str:
    return prefix + secrets.token_hex(12)


def _save_setup(setup: dict) -> dict:
    state = _read_state()
    setups = dict(state.get("setups") or {})
    setups[setup["id"]] = setup
    _write_state({**state, "setups": setups})
    return dict(setup)


def _load_setup(setup_id: str) -> dict:
    setup = (_read_state().get("setups") or {}).get(setup_id)
    if setup is None:
        raise FakeStripeError(f"No such setup_intent: {setup_id}")
    return dict(setup)


class FakeStripe:
    """Answers like the Stripe endpoints ``client.StripeClient`` calls."""

    def create_customer(
        self, *, name: str, email: str = "", metadata: dict, idempotency_key: str = ""
    ) -> dict:
        return {"id": _token("cus_"), "object": "customer", "name": name, "metadata": metadata}

    def create_setup_intent(
        self, *, customer: str, metadata: dict, idempotency_key: str = ""
    ) -> dict:
        sid = _token("seti_")
        return _save_setup(
            {
                "id": sid,
                "object": "setup_intent",
                "client_secret": f"{sid}_secret_{secrets.token_hex(8)}",
                "customer": customer,
                "status": "requires_payment_method",
                "payment_method": None,
                "mandate": None,
                "next_action": None,
                "metadata": metadata,
                "homeoa_mode": "",
            }
        )

    def collect_bank_account(self, setup_id: str, mode: str) -> dict:
        """Fake-only: what Stripe.js ``collectBankAccountForSetup`` does in the browser."""
        setup = _load_setup(setup_id)
        if mode == "fail":
            raise FakeStripeError("The bank account could not be linked (fake failure).")
        if mode not in ("instant", "manual"):
            raise FakeStripeError("unknown mode")
        setup["payment_method"] = {
            "id": _token("pm_"),
            "object": "payment_method",
            "type": "us_bank_account",
            "us_bank_account": {"last4": FAKE_LAST4, "bank_name": FAKE_BANK},
        }
        setup["status"] = "requires_confirmation"
        setup["homeoa_mode"] = mode
        return _save_setup(setup)

    def retrieve_setup_intent(self, setup_id: str) -> dict:
        return _load_setup(setup_id)

    def confirm_setup_intent(
        self, setup_id: str, *, ip_address: str, user_agent: str, idempotency_key: str = ""
    ) -> dict:
        setup = _load_setup(setup_id)
        if not (ip_address and user_agent):
            raise FakeStripeError("An online mandate needs the owner's IP address and browser.")
        if setup["status"] != "requires_confirmation":
            raise FakeStripeError("This bank account is not ready to confirm.")
        setup["homeoa_acceptance"] = {"type": "online", "ip_address": ip_address}
        if setup["homeoa_mode"] == "instant":
            setup["status"] = "succeeded"
            setup["mandate"] = _token("mandate_")
        else:
            setup["status"] = "requires_action"
            setup["next_action"] = {
                "type": "verify_with_microdeposits",
                "verify_with_microdeposits": {"microdeposit_type": "amounts"},
            }
        return _save_setup(setup)

    def verify_microdeposits(
        self,
        setup_id: str,
        *,
        amounts: list[int] | None = None,
        descriptor_code: str = "",
        idempotency_key: str = "",
    ) -> dict:
        setup = _load_setup(setup_id)
        if setup["status"] != "requires_action":
            raise FakeStripeError("This bank account is not waiting for microdeposits.")
        ok = (descriptor_code or "").strip().upper() == TEST_DESCRIPTOR_CODE or sorted(
            int(a) for a in amounts or []
        ) == TEST_AMOUNTS
        if not ok:
            raise FakeStripeError("The amounts provided do not match the microdeposits.")
        setup["status"] = "succeeded"
        setup["next_action"] = None
        setup["mandate"] = _token("mandate_")
        return _save_setup(setup)

    def create_payment_intent(
        self,
        *,
        amount: int,
        metadata: dict,
        mandate: str,
        customer: str,
        payment_method: str,
        idempotency_key: str = "",
    ) -> dict:
        if not (mandate and customer and payment_method):
            raise FakeStripeError("A debit needs a stored mandate, customer and payment method.")
        state = _read_state()
        if state.get("fail_next"):
            _write_state({**state, "fail_next": False})
            raise FakeStripeError("The bank account could not be debited (fake failure).")
        if amount <= 0:
            raise FakeStripeError("amount must be positive")
        return {
            "id": "pi_" + secrets.token_hex(12),
            "object": "payment_intent",
            "amount": amount,
            "currency": "usd",
            "status": "processing",
            "mandate": mandate,
            "customer": customer,
            "payment_method": payment_method,
            "metadata": metadata,
        }


def event(event_type: str, intent: dict, *, fee: int = 0, event_id: str = "") -> bytes:
    """A Stripe-shaped event body. ``fee`` (cents) stands in for the balance transaction."""
    obj = {**intent, "homeoa_fee": fee}
    return json.dumps(
        {
            "id": event_id or "evt_" + secrets.token_hex(12),
            "object": "event",
            "type": event_type,
            "data": {"object": obj},
        }
    ).encode()


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """A ``Stripe-Signature`` header value."""
    ts = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"
