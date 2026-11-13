"""Which Stripe client to use: the real HTTP client or the in-repo fake.

``settings.HOMEOA_STRIPE_CLIENT`` is ``"live"`` (the default, and forced in production) or
``"fake"``. The fake is only reachable when the fixed clock is allowed (test/e2e/dev
settings) or ``DEBUG`` is on; anything else raises ``ImproperlyConfigured`` so production
can never record fake payments.

Keys: the installation's ``api_key`` secret, else ``settings.HOMEOA_STRIPE_SECRET_KEY``
(from the ``HOMEOA_STRIPE_SECRET_KEY`` environment variable). Only Stripe **test-mode**
keys are accepted (``sk_test_`` / ``rk_test_`` secret keys, ``pk_test_`` publishable):
``check_test_key`` refuses anything else, at app start-up for the settings keys and again
when a client is built. Live keys are out of scope.

``StripeClient`` speaks the same small surface as ``fake.FakeStripe`` over the Stripe REST
API. HomeOA calls nothing that sends money out (no payouts, no refunds). Unit tests never
exercise its network path; ``tests/test_live_stripe.py`` (``-m live_stripe``) does.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

API_BASE = "https://api.stripe.com/v1"
SECRET_KEY_PREFIXES = ("sk_test_", "rk_test_")
PUBLISHABLE_KEY_PREFIXES = ("pk_test_",)


FC_PERMISSIONS = "payment_method_options[us_bank_account][financial_connections][permissions][]"


class StripeError(Exception):
    pass


def fake_allowed() -> bool:
    return bool(getattr(settings, "HOMEOA_FIXED_TODAY_ALLOWED", False) or settings.DEBUG)


def check_test_key(key: str, *, name: str = "Stripe key", secret: bool = True) -> str:
    """Return ``key`` if it is a Stripe test-mode key; raise ``ImproperlyConfigured`` if not.

    The message never contains the key itself."""
    allowed = SECRET_KEY_PREFIXES if secret else PUBLISHABLE_KEY_PREFIXES
    if not key.startswith(allowed):
        live = "_live_" in key[:9]
        raise ImproperlyConfigured(
            f"{name} is not a Stripe test-mode key (expected {' or '.join(allowed)}). "
            + ("Live keys are not supported." if live else "Check the value.")
        )
    return key


def check_settings_keys() -> None:
    """Refuse to start with a non-test key in settings (called from ``apps.ready``)."""
    for attr, secret in (
        ("HOMEOA_STRIPE_SECRET_KEY", True),
        ("HOMEOA_STRIPE_PUBLISHABLE_KEY", False),
    ):
        value = getattr(settings, attr, "") or ""
        if value:
            check_test_key(value, name=attr, secret=secret)


class StripeClient:
    def __init__(self, api_key: str, *, timeout: int = 20):
        if not api_key:
            raise ImproperlyConfigured("Stripe is not set up with an API key.")
        self.api_key = check_test_key(api_key, name="Stripe API key")
        self.timeout = timeout

    def _request(
        self, method: str, path: str, data: dict | None = None, *, idempotency_key: str = ""
    ) -> dict:
        url = API_BASE + path
        body = None
        if method == "POST":
            body = urllib.parse.urlencode(data or {}, doseq=True).encode()
        elif data:
            url += "?" + urllib.parse.urlencode(data, doseq=True)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read())["error"]["message"]
            except Exception:
                message = f"HTTP {exc.code}"
            raise StripeError(message) from exc

    def _post(self, path: str, data: dict, *, idempotency_key: str = "") -> dict:
        return self._request("POST", path, data, idempotency_key=idempotency_key)

    # ------------------------------------------------------------ bank account setup

    def create_customer(
        self, *, name: str, email: str = "", metadata: dict, idempotency_key: str = ""
    ) -> dict:
        data = {"name": name}
        if email:
            data["email"] = email
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = str(value)
        return self._post("/customers", data, idempotency_key=idempotency_key)

    def create_setup_intent(
        self, *, customer: str, metadata: dict, idempotency_key: str = ""
    ) -> dict:
        """A SetupIntent for a US bank account. ``verification_method=automatic``: Stripe
        Financial Connections (instant) first, microdeposits when the bank isn't supported
        or the owner enters account numbers by hand."""
        data = {
            "customer": customer,
            "usage": "off_session",
            "payment_method_types[]": "us_bank_account",
            "payment_method_options[us_bank_account][verification_method]": "automatic",
            FC_PERMISSIONS: "payment_method",
        }
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = str(value)
        return self._post("/setup_intents", data, idempotency_key=idempotency_key)

    def retrieve_setup_intent(self, setup_id: str) -> dict:
        return self._request(
            "GET",
            f"/setup_intents/{urllib.parse.quote(setup_id)}",
            {"expand[]": "payment_method"},
        )

    def confirm_setup_intent(
        self, setup_id: str, *, ip_address: str, user_agent: str, idempotency_key: str = ""
    ) -> dict:
        """Confirm with the owner's **online** acceptance of the mandate (never offline)."""
        if not (ip_address and user_agent):
            raise StripeError("An online mandate needs the owner's IP address and browser.")
        data = {
            "mandate_data[customer_acceptance][type]": "online",
            "mandate_data[customer_acceptance][online][ip_address]": ip_address,
            "mandate_data[customer_acceptance][online][user_agent]": user_agent,
            "expand[]": "payment_method",
        }
        return self._post(
            f"/setup_intents/{urllib.parse.quote(setup_id)}/confirm",
            data,
            idempotency_key=idempotency_key,
        )

    def verify_microdeposits(
        self,
        setup_id: str,
        *,
        amounts: list[int] | None = None,
        descriptor_code: str = "",
        idempotency_key: str = "",
    ) -> dict:
        data: dict = {"expand[]": "payment_method"}
        if descriptor_code:
            data["descriptor_code"] = descriptor_code
        else:
            data["amounts[]"] = [int(a) for a in amounts or []]
        return self._post(
            f"/setup_intents/{urllib.parse.quote(setup_id)}/verify_microdeposits",
            data,
            idempotency_key=idempotency_key,
        )

    # ------------------------------------------------------------ debits

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
        """Create and confirm an ACH debit against a stored, verified mandate.

        Every debit references the owner's mandate: there is no offline-acceptance path."""
        if not (mandate and customer and payment_method):
            raise StripeError("A debit needs a stored mandate, customer and payment method.")
        data = {
            "amount": amount,
            "currency": "usd",
            "confirm": "true",
            "off_session": "true",
            "payment_method_types[]": "us_bank_account",
            "customer": customer,
            "payment_method": payment_method,
            "mandate": mandate,
        }
        for key, value in metadata.items():
            data[f"metadata[{key}]"] = str(value)
        return self._post("/payment_intents", data, idempotency_key=idempotency_key)

    def retrieve_payment_intent(self, intent_id: str, *, expand: str = "") -> dict:
        params = {"expand[]": expand} if expand else None
        return self._request("GET", f"/payment_intents/{urllib.parse.quote(intent_id)}", params)

    def intent_fee(self, intent_id: str) -> int | None:
        """The actual fee in cents from the intent's balance transaction (None if not yet).

        Real ``payment_intent.succeeded`` events carry ``latest_charge`` as an id only, so
        the fee needs this one extra read (the fake puts it in ``homeoa_fee`` instead)."""
        data = self.retrieve_payment_intent(intent_id, expand="latest_charge.balance_transaction")
        return fee_from_intent(data)


def fee_from_intent(data: dict) -> int | None:
    charge = data.get("latest_charge")
    txn = charge.get("balance_transaction") if isinstance(charge, dict) else None
    fee = txn.get("fee") if isinstance(txn, dict) else None
    return fee if isinstance(fee, int) else None


def get_client(installation=None):
    """The configured client for an installation (``None``: no installation yet)."""
    kind = getattr(settings, "HOMEOA_STRIPE_CLIENT", "live")
    if kind == "fake":
        if not fake_allowed():
            raise ImproperlyConfigured("The fake Stripe client is not allowed in production.")
        from .fake import FakeStripe

        return FakeStripe()
    secrets = (installation.secrets or {}) if installation is not None else {}
    key = secrets.get("api_key") or getattr(settings, "HOMEOA_STRIPE_SECRET_KEY", "")
    return StripeClient(key)
