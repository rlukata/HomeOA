"""Opt-in smoke test of the real client against Stripe **test mode** (sandbox).

Not part of ``scripts/check.sh``, CI or E2E: ``addopts`` deselects ``live_stripe``. Run::

    HOMEOA_ENV_FILE=/path/to/.env uv run pytest -m live_stripe

The key comes from ``HOMEOA_STRIPE_SECRET_KEY`` in the environment or that ``.env`` (default
``<project>/.env``); it must be a test-mode key. Never print or commit it.

Steps, through the same code the owner's bank page uses (``bank.py``):

1. ``bank.start``: a test customer and a SetupIntent (``verification_method=automatic``).
2. Stand in for Stripe.js: attach Stripe's documented test account numbers (routing
   110000000, account 000123456789) to the SetupIntent. Hand-entered numbers fall back to
   microdeposits.
3. ``bank.confirm`` with an online acceptance (IP and browser), then ``bank.verify`` with
   the test amounts 32/45: the mandate is now chargeable.
4. ``services.create_payment`` for unit C's dues under that mandate.
5. The webhook. With the Stripe CLI installed (``stripe``), ``stripe listen`` forwards the
   real ``payment_intent.succeeded`` event to this test's live server, signed with the
   CLI's secret; the test waits (bounded) for the ledger to show it. The key reaches the
   CLI only through its ``STRIPE_API_KEY`` environment variable, never argv, and nothing
   prints it. Without the CLI, the test polls the intent and posts a real-shaped event
   signed locally with a throwaway secret.
6. Check the ledger: receipt into 1060 clearing, the real balance-transaction fee in 5450.

Budget: about 15 requests per run, every POST with an idempotency key where the client
supports one, no retries; only the status poll repeats, at most ``POLLS`` times.
Collections only: no payouts, no refunds.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from homeoa.plugins.services import installation_for
from homeoa.registry.services import current_owners
from homeoa_stripe import bank, fake
from homeoa_stripe import client as sc
from homeoa_stripe.models import IntentStatus, StripeIntent, StripeMandate
from homeoa_stripe.services import create_payment
from homeoa_stripe.webhook import SIGNATURE_HEADER

from .test_stripe import DUES_C

pytestmark = pytest.mark.live_stripe

KEY = "HOMEOA_STRIPE_SECRET_KEY"
POLLS = 15
POLL_SECONDS = 4
EVENTS = "payment_intent.succeeded,payment_intent.payment_failed,charge.failed"


def _key() -> str:
    if os.environ.get(KEY):
        return os.environ[KEY]
    default = Path(__file__).resolve().parents[4] / ".env"
    path = Path(os.environ.get("HOMEOA_ENV_FILE") or default)
    try:
        for line in path.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == KEY:
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


@pytest.fixture
def stripe_key():
    key = _key()
    if not key:
        pytest.skip(f"{KEY} not set")
    return sc.check_test_key(key, name=KEY)


def _cli_env(key: str) -> dict:
    """The CLI reads its key from ``STRIPE_API_KEY``; never pass it on the command line."""
    return {**os.environ, "STRIPE_API_KEY": key}


@pytest.fixture
def stripe_listen(stripe_key, live_server, pilot):
    """Forward real events to the live server; yields the CLI's webhook secret (or None)."""
    cli = shutil.which("stripe")
    if cli is None:
        yield None
        return
    env = _cli_env(stripe_key)
    secret = subprocess.run(
        [cli, "listen", "--print-secret"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()
    assert secret.startswith("whsec_"), "stripe listen did not return a webhook secret"
    url = f"{live_server.url}/hooks/stripe/{pilot.pk}/"
    proc = subprocess.Popen(
        [cli, "listen", "--forward-to", url, "--events", EVENTS],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:  # "Ready!" is printed on stderr
            line = proc.stderr.readline()
            if "Ready" in line or not line:
                break
        yield secret
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _link_bank(real, unit, owner, run):
    started = bank.start(unit, owner, client=real)
    # What Stripe.js would do in the browser: hand-entered test numbers.
    real._post(
        f"/setup_intents/{started['setup_id']}",
        {
            "payment_method_data[type]": "us_bank_account",
            "payment_method_data[us_bank_account][account_holder_type]": "individual",
            "payment_method_data[us_bank_account][routing_number]": "110000000",
            "payment_method_data[us_bank_account][account_number]": "000123456789",
            "payment_method_data[billing_details][name]": "HomeOA Test Owner",
        },
        idempotency_key=f"homeoa-live-{run}-attach",
    )
    assert bank.collected_account(started["setup_id"], association=unit.association, client=real)
    mandate = bank.confirm(
        unit,
        owner,
        started["setup_id"],
        ip_address="203.0.113.5",
        user_agent="HomeOA live test",
        client=real,
    )
    if not mandate.chargeable:
        mandate = bank.verify(mandate, amounts=[32, 45], client=real)
    assert mandate.chargeable
    assert mandate.stripe_mandate_id and mandate.stripe_payment_method_id
    return mandate


def _wait_succeeded(real, intent) -> dict:
    data = {}
    for _ in range(POLLS):
        data = real.retrieve_payment_intent(intent.intent_id)
        if data["status"] != "processing":
            break
        time.sleep(POLL_SECONDS)
    assert data["status"] == "succeeded", data["status"]
    return data


def _post_locally(client, pilot, data, run):
    secret = "whsec_" + uuid.uuid4().hex
    inst = installation_for(pilot, "stripe")
    inst.secrets = {**(inst.secrets or {}), "webhook_secret": secret}
    inst.save()
    body = json.dumps(
        {
            "id": f"evt_live_{run}",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": data},
        }
    ).encode()
    resp = client.post(
        f"/hooks/stripe/{pilot.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(body, secret)},
    )
    assert resp.status_code == 200


def test_bank_link_debit_and_webhook(
    transactional_db, client, pilot, unit_c, settings, stripe_key, stripe_listen
):
    settings.HOMEOA_STRIPE_CLIENT = "live"
    settings.HOMEOA_STRIPE_SECRET_KEY = stripe_key
    if stripe_listen:
        inst = installation_for(pilot, "stripe")
        inst.secrets = {**(inst.secrets or {}), "webhook_secret": stripe_listen}
        inst.save()
    real = sc.StripeClient(stripe_key)
    run = uuid.uuid4().hex[:12]
    owner = current_owners(unit_c)[0]
    # The demo seeds fake-shaped bank accounts for C; they would lend a fake customer id.
    # Mandates are never deleted; revoking them keeps them out of the way.
    from homeoa_stripe.mandates import revoke_mandate

    for old in StripeMandate.objects.filter(unit=unit_c):
        revoke_mandate(old)

    mandate = _link_bank(real, unit_c, owner, run)
    intent = create_payment(
        unit_c, DUES_C, mandate, client=real, idempotency_key=f"homeoa-live-{run}"
    )
    assert intent.status == IntentStatus.PROCESSING, intent.message
    assert intent.intent_id.startswith("pi_")
    assert intent.charged == DUES_C  # absorb mode: Stripe is asked for the dues only

    data = _wait_succeeded(real, intent)
    assert data["amount"] == fake.to_cents(DUES_C) and data["currency"] == "usd"
    assert data["metadata"]["unit"] == "C"

    if stripe_listen:
        for _ in range(POLLS):  # the CLI forwards the event to the live server
            intent = StripeIntent.objects.get(pk=intent.pk)
            if intent.status == IntentStatus.SUCCEEDED:
                break
            time.sleep(POLL_SECONDS)
    else:
        _post_locally(client, pilot, data, run)
        intent.refresh_from_db()
    assert intent.status == IntentStatus.SUCCEEDED
    assert intent.receipt.deposit_account.code == "1060"
    actual = real.intent_fee(intent.intent_id)
    assert actual is not None
    assert intent.actual_fee == fake.from_cents(actual)
    # The fake's price model (0.8%, cap $5) should match what Stripe charged.
    assert intent.actual_fee == fake.ach_fee(DUES_C) == intent.fee
    lines = {(ln.account.code, ln.debit, ln.credit) for ln in intent.fee_entry.lines.all()}
    assert lines == {
        ("5450", intent.actual_fee, Decimal("0.00")),
        ("1060", Decimal("0.00"), intent.actual_fee),
    }
