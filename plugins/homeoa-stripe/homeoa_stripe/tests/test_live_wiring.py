"""Test-mode key guard, real-client request shape (no network) and webhook fee fallback."""

import json
import urllib.parse
import urllib.request
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured

from homeoa_stripe import client as sc
from homeoa_stripe import fake
from homeoa_stripe.models import IntentStatus
from homeoa_stripe.provider import StripeACH
from homeoa_stripe.webhook import SIGNATURE_HEADER

from .test_stripe import DUES_C, SECRET, pay

# Obviously synthetic placeholders, never real keys.
SK = "sk_test_" + "x" * 24
LIVE = "sk_live_" + "x" * 24


@pytest.mark.parametrize("key", [SK, "rk_test_" + "x" * 24])
def test_secret_test_keys_accepted(key):
    assert sc.StripeClient(key).api_key == key


@pytest.mark.parametrize("key", [LIVE, "rk_live_abc", "pk_test_abc", "whatever", "sk_testx"])
def test_non_test_secret_keys_refused(key):
    with pytest.raises(ImproperlyConfigured) as exc:
        sc.StripeClient(key)
    assert key not in str(exc.value)
    if "_live_" in key:
        assert "Live keys are not supported" in str(exc.value)


def test_startup_guard_checks_settings_keys(settings):
    settings.HOMEOA_STRIPE_SECRET_KEY = SK
    settings.HOMEOA_STRIPE_PUBLISHABLE_KEY = "pk_test_abc"
    sc.check_settings_keys()
    settings.HOMEOA_STRIPE_PUBLISHABLE_KEY = "pk_live_abc"
    with pytest.raises(ImproperlyConfigured, match="HOMEOA_STRIPE_PUBLISHABLE_KEY"):
        sc.check_settings_keys()
    settings.HOMEOA_STRIPE_PUBLISHABLE_KEY = ""
    settings.HOMEOA_STRIPE_SECRET_KEY = LIVE
    with pytest.raises(ImproperlyConfigured, match="HOMEOA_STRIPE_SECRET_KEY"):
        sc.check_settings_keys()


def test_app_ready_runs_guard(settings):
    from django.apps import apps

    settings.HOMEOA_STRIPE_SECRET_KEY = LIVE
    with pytest.raises(ImproperlyConfigured):
        apps.get_app_config("homeoa_stripe").ready()


def test_get_client_reads_settings_key(settings):
    settings.HOMEOA_STRIPE_CLIENT = "live"
    settings.HOMEOA_STRIPE_SECRET_KEY = SK
    assert isinstance(sc.get_client(None), sc.StripeClient)
    settings.HOMEOA_STRIPE_SECRET_KEY = ""
    with pytest.raises(ImproperlyConfigured):
        sc.get_client(None)


def test_provider_client_is_lazy(settings):
    settings.HOMEOA_STRIPE_CLIENT = "live"
    settings.HOMEOA_STRIPE_SECRET_KEY = ""
    provider = StripeACH()  # building the provider needs no key
    with pytest.raises(ImproperlyConfigured):
        provider.client  # noqa: B018


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps(self.data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_create_intent_request_shape(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["req"] = req
        return _Resp({"id": "pi_1", "status": "processing"})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    sc.StripeClient(SK).create_payment_intent(
        amount=39663,
        metadata={"unit": "C"},
        mandate="mandate_1",
        customer="cus_1",
        payment_method="pm_1",
        idempotency_key="homeoa-test-1",
    )
    req = seen["req"]
    body = dict(p.split("=", 1) for p in req.data.decode().split("&"))
    assert body["amount"] == "39663" and body["confirm"] == "true"
    assert body["customer"] == "cus_1" and body["payment_method"] == "pm_1"
    assert body["mandate"] == "mandate_1"
    assert not any("customer_acceptance" in k for k in body)  # never offline acceptance
    assert req.get_header("Idempotency-key") == "homeoa-test-1"
    assert "payout" not in req.full_url and "refund" not in req.full_url


def test_debit_without_mandate_refused(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("no request"))
    with pytest.raises(sc.StripeError):
        sc.StripeClient(SK).create_payment_intent(
            amount=100, metadata={}, mandate="", customer="cus_1", payment_method="pm_1"
        )


def test_setup_intent_confirm_is_online(monkeypatch):
    seen = []

    def urlopen(req, timeout):
        seen.append(req)
        return _Resp({"id": "seti_1", "status": "succeeded"})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    c = sc.StripeClient(SK)
    c.create_setup_intent(customer="cus_1", metadata={"unit": "C"})
    c.confirm_setup_intent("seti_1", ip_address="203.0.113.5", user_agent="Mozilla/5.0")
    setup = urllib.parse.unquote(seen[0].data.decode())
    assert "verification_method]=automatic" in setup
    confirm = urllib.parse.unquote(seen[1].data.decode())
    assert seen[1].full_url.endswith("/setup_intents/seti_1/confirm")
    assert "[customer_acceptance][type]=online" in confirm
    assert "offline" not in confirm
    with pytest.raises(sc.StripeError):
        c.confirm_setup_intent("seti_1", ip_address="", user_agent="")


def test_fee_from_expanded_intent():
    data = {"latest_charge": {"balance_transaction": {"fee": 317}}}
    assert sc.fee_from_intent(data) == 317
    assert sc.fee_from_intent({"latest_charge": "ch_1"}) is None


def test_real_shaped_event_reads_fee_from_client(client, pilot, unit_c, monkeypatch):
    """A real event has no ``homeoa_fee``: the webhook asks the client for the fee."""
    intent = pay(unit_c, DUES_C)

    class Reader:
        def intent_fee(self, intent_id):
            assert intent_id == intent.intent_id
            return 300  # actual differs from the 3.17 estimate

    monkeypatch.setattr(sc, "get_client", lambda installation=None: Reader())
    body = json.dumps(
        {
            "id": "evt_real_shape",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "id": intent.intent_id,
                    "object": "payment_intent",
                    "latest_charge": "ch_1",
                }
            },
        }
    ).encode()
    resp = client.post(
        f"/hooks/stripe/{pilot.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(body, SECRET)},
    )
    assert resp.status_code == 200
    intent.refresh_from_db()
    assert intent.status == IntentStatus.SUCCEEDED
    assert intent.actual_fee == Decimal("3.00")
    codes = {(ln.account.code, ln.debit, ln.credit) for ln in intent.fee_entry.lines.all()}
    assert codes == {
        ("5450", Decimal("3.00"), Decimal("0.00")),
        ("1060", Decimal("0.00"), Decimal("3.00")),
    }


def test_signature_with_wrong_secret_rejected(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    body = fake.event("payment_intent.succeeded", {"id": intent.intent_id}, fee=317)
    resp = client.post(
        f"/hooks/stripe/{pilot.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(body, "whsec_other_test_secret")},
    )
    assert resp.status_code in (400, 403)
    intent.refresh_from_db()
    assert intent.status == IntentStatus.PROCESSING
