"""Mandates, returns, payouts, fee differences, client selection."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Sum

from homeoa import rules
from homeoa.banking import matching
from homeoa.banking import services as banking
from homeoa.banking.models import BankAccount, BankTransaction, TxnStatus
from homeoa.dashboard import todos
from homeoa.ledger.models import JournalLine
from homeoa.payments.models import Method, ReceiptStatus
from homeoa.plugins import services as plugins
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.registry.models import Person
from homeoa_stripe import client as stripe_client
from homeoa_stripe import fake, mandates, services
from homeoa_stripe.models import FeeMode, IntentStatus, StripePayout
from homeoa_stripe.services import create_payment
from homeoa_stripe.webhook import SIGNATURE_HEADER

from .test_stripe import DUES_C, SECRET, c_balance, pay, post

D = Decimal


def hook(client, assoc, event_type, obj, event_id=""):
    body = fake.event(event_type, obj, event_id=event_id)
    return client.post(
        f"/hooks/stripe/{assoc.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(body, SECRET)},
    )


def post_fee(client, assoc, intent, fee_cents):
    body = fake.event(
        "payment_intent.succeeded", {"id": intent.intent_id}, fee=fee_cents, event_id=""
    )
    return client.post(
        f"/hooks/stripe/{assoc.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(body, SECRET)},
    )


def login_board(client, assoc):
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("stripe-board@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return user


def clearing_balance():
    agg = JournalLine.objects.filter(account__code="1060").aggregate(
        d=Sum("debit"), c=Sum("credit")
    )
    return (agg["d"] or D("0")) - (agg["c"] or D("0"))


# ------------------------------------------------------------------ method + client


def test_receipt_method_is_ach(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    post(client, pilot, intent)
    intent.refresh_from_db()
    assert intent.receipt.method == Method.ACH


def test_manual_receipt_form_hides_ach():
    from homeoa.payments.forms import ReceiptForm

    assert "ach" not in dict(ReceiptForm.base_fields["method"].choices)


def test_fake_client_under_test_settings(pilot):
    assert isinstance(stripe_client.get_client(), fake.FakeStripe)


def test_prod_settings_refuse_fake(settings):
    settings.DEBUG = False
    settings.HOMEOA_FIXED_TODAY_ALLOWED = False
    settings.HOMEOA_STRIPE_CLIENT = "fake"
    with pytest.raises(ImproperlyConfigured):
        stripe_client.get_client()
    settings.HOMEOA_STRIPE_CLIENT = "live"
    settings.HOMEOA_STRIPE_SECRET_KEY = ""  # .env may supply a test key
    with pytest.raises(ImproperlyConfigured):  # no key yet
        stripe_client.get_client()


def test_prod_settings_module_refuses_fake(monkeypatch):
    import importlib
    import sys

    monkeypatch.setenv("HOMEOA_SECRET_KEY", "x" * 50)
    monkeypatch.setenv("HOMEOA_FIELD_KEYS", "q0ruyRMbeq9vb8AUn1zcCbvB9q4yqBu0yT0QaB_ocBc=")
    monkeypatch.setenv("HOMEOA_STRIPE_CLIENT", "fake")
    monkeypatch.delenv("HOMEOA_FIXED_TODAY", raising=False)
    sys.modules.pop("homeoa.settings.prod", None)
    sys.modules.pop("homeoa.settings.base", None)
    with pytest.raises(ImproperlyConfigured, match="HOMEOA_STRIPE_CLIENT"):
        importlib.import_module("homeoa.settings.prod")
    sys.modules.pop("homeoa.settings.prod", None)
    sys.modules.pop("homeoa.settings.base", None)
    monkeypatch.setenv("HOMEOA_STRIPE_CLIENT", "live")
    prod = importlib.import_module("homeoa.settings.prod")
    assert prod.HOMEOA_STRIPE_CLIENT == "live" and prod.HOMEOA_FIXED_TODAY_ALLOWED is False
    sys.modules.pop("homeoa.settings.prod", None)


def test_live_client_with_key_builds_without_network(settings):
    settings.HOMEOA_STRIPE_CLIENT = "live"

    class Inst:
        secrets = {"api_key": "sk_test_dummy"}

    c = stripe_client.get_client(Inst())
    assert isinstance(c, stripe_client.StripeClient) and c.api_key == "sk_test_dummy"


# ------------------------------------------------------------------ rule key


def test_rule_key_unset_and_unverified_in_wa(pilot):
    from homeoa_rulepack_us_wa.pack import pack as wa_pack

    # WA allows pass-through (attorney review); the board must still opt in.
    wa = wa_pack.get("payments.fee_pass_through_allowed", dt.date(2026, 10, 7))
    assert wa.value is True and wa.attorney_review
    assert services.fee_mode(pilot) == FeeMode.ABSORB


def test_rule_override_off_wins_over_opt_in(pilot, unit_c):
    plugins.save_installation(
        pilot, "stripe", enabled=True, data={"webhook_secret": SECRET, "fee_mode": "pass_through"}
    )
    rules.set_override(pilot, "payments.fee_pass_through_allowed", False, reason="board")
    assert pay(unit_c, DUES_C).fee_mode == FeeMode.ABSORB
    rules.set_override(pilot, "payments.fee_pass_through_allowed", True, reason="board")
    assert pay(unit_c, DUES_C).fee_mode == FeeMode.PASS_THROUGH


def test_rule_shows_on_settings_page(client, pilot):
    login_board(client, pilot)
    resp = client.get("/settings/rules/")
    assert resp.status_code == 200
    assert b"payments.fee_pass_through_allowed" in resp.content or (
        b"processing fee" in resp.content
    )


# ------------------------------------------------------------------ mandates


def test_mandate_recorded_and_revoked(pilot, unit_c):
    owner = Person.objects.filter(full_name__icontains="Priya").first() or Person.objects.first()
    m = mandates.record_mandate(
        unit_c, owner, "6789", ip_address="203.0.113.5", user_agent="Mozilla/5.0"
    )
    assert m.authorization_version == mandates.CURRENT_VERSION
    assert "6789" in m.authorization_text and "unit C" in m.authorization_text
    assert m.accepted_at and m.revoked_at is None
    from homeoa.payments.services import PaymentError

    with pytest.raises(PaymentError):  # not verified, no Stripe ids: can't be charged
        create_payment(unit_c, DUES_C, m)
    m.stripe_mandate_id = "mandate_1"
    m.stripe_customer_id, m.stripe_payment_method_id = "cus_1", "pm_1"
    m.verified_at = m.accepted_at
    m.save()
    intent = create_payment(unit_c, DUES_C, m)
    assert intent.mandate_record == m and intent.mandate == "mandate_1"
    mandates.revoke_mandate(m)
    m.refresh_from_db()
    assert m.revoked_at is not None
    with pytest.raises(PaymentError):
        create_payment(unit_c, DUES_C, m)


def test_mandate_needs_last4(pilot, unit_c):
    with pytest.raises(mandates.MandateError):
        mandates.record_mandate(unit_c, Person.objects.first(), "12a")


# ------------------------------------------------------------------ fees


def test_absorbed_fee_uses_actual(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)  # estimate 3.17
    post_fee(client, pilot, intent, 290)
    intent.refresh_from_db()
    assert intent.fee == D("3.17") and intent.actual_fee == D("2.90")
    assert clearing_balance() == DUES_C - D("2.90")


def test_pass_through_fee_difference_booked(client, pilot, unit_c):
    plugins.save_installation(
        pilot, "stripe", enabled=True, data={"webhook_secret": SECRET, "fee_mode": "pass_through"}
    )
    rules.set_override(pilot, "payments.fee_pass_through_allowed", True, reason="test")
    hi = pay(unit_c, DUES_C)  # owner charged 399.80
    post_fee(client, pilot, hi, 350)  # Stripe took 3.50: 0.33 more than estimated
    hi.refresh_from_db()
    lines = {(ln.account.code, ln.debit, ln.credit) for ln in hi.fee_entry.lines.all()}
    assert lines == {("5450", D("0.33"), D("0.00")), ("1060", D("0.00"), D("0.33"))}
    lo = pay(unit_c, DUES_C)
    post_fee(client, pilot, lo, 300)  # 0.17 less
    lo.refresh_from_db()
    lines = {(ln.account.code, ln.debit, ln.credit) for ln in lo.fee_entry.lines.all()}
    assert lines == {("1060", D("0.17"), D("0.00")), ("5450", D("0.00"), D("0.17"))}
    exact = pay(unit_c, DUES_C)
    post_fee(client, pilot, exact, 317)
    exact.refresh_from_db()
    assert exact.fee_entry is None
    # 1060 holds receipts net of the fee difference = what Stripe will pay out.
    assert clearing_balance() == 3 * DUES_C - D("0.33") + D("0.17")


# ------------------------------------------------------------------ returns


def test_return_after_success_voids_and_todos(client, pilot, unit_c):
    before = c_balance(unit_c)
    intent = pay(unit_c, DUES_C)
    post(client, pilot, intent)
    assert c_balance(unit_c) == before - DUES_C
    obj = {
        "id": intent.intent_id,
        "object": "payment_intent",
        "last_payment_error": {"message": "R01 insufficient funds"},
    }
    assert hook(client, pilot, "payment_intent.payment_failed", obj).status_code == 200
    intent.refresh_from_db()
    assert intent.status == IntentStatus.FAILED and intent.returned_at is not None
    assert intent.receipt.status == ReceiptStatus.VOID
    assert c_balance(unit_c) == before
    items = [i for i in todos.collect(pilot) if i.id == f"stripe-return-{intent.pk}"]
    assert len(items) == 1
    assert items[0].title == "ACH payment of $396.63 from unit C on Oct 7 failed"
    # Replayed failure (new event id) is a no-op.
    hook(client, pilot, "payment_intent.payment_failed", obj)
    from homeoa.payments.models import Receipt

    assert Receipt.objects.filter(reference=intent.intent_id, status="void").count() == 1
    # Fee stays expensed; clearing nets to minus the fee Stripe kept.
    assert clearing_balance() == -intent.actual_fee
    from homeoa.ledger.services import trial_balance

    tb = trial_balance()
    assert tb.total_debit == tb.total_credit


def test_return_via_charge_failed_and_ack(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    post(client, pilot, intent)
    hook(
        client,
        pilot,
        "charge.failed",
        {"id": "ch_1", "payment_intent": intent.intent_id, "failure_message": "R02"},
    )
    intent.refresh_from_db()
    assert intent.returned_at is not None
    login_board(client, pilot)
    resp = client.post(f"/plugins/stripe/returns/{intent.pk}/ack/")
    assert resp.status_code == 302
    assert not [i for i in todos.collect(pilot) if i.id.startswith("stripe-return-")]


# ------------------------------------------------------------------ payouts


def _deposit(assoc, amount, date, desc="STRIPE TRANSFER"):
    account = BankAccount.objects.get(gl_account__code="1000")
    feed = FeedTxn(external_id=f"po-{amount}-{date}", posted=date, amount=amount, description=desc)
    banking.import_transactions(account, [feed], source="csv")
    return BankTransaction.objects.get(bank_account=account, external_id=feed.external_id)


def test_payout_match_moves_clearing_to_cash(client, pilot):
    from homeoa.registry.models import Unit

    for number in ("A", "B"):
        intent = pay(Unit.objects.get(number=number), D("500.00"))
        post(client, pilot, intent)
    net = clearing_balance()
    assert net == D("1000.00") - 2 * fake.ach_fee(D("500.00"))
    payout = {
        "id": "po_123",
        "object": "payout",
        "amount": fake.to_cents(net),
        "arrival_date": int(dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC).timestamp()),
    }
    assert hook(client, pilot, "payout.paid", payout).status_code == 200
    hook(client, pilot, "payout.paid", payout)  # replay
    assert StripePayout.objects.count() == 1
    p = StripePayout.objects.get()
    assert p.amount == net and p.arrival_date == dt.date(2026, 10, 9)
    # The 1060 balance equals the sum of open payouts.
    assert clearing_balance() == sum(x.amount for x in services.open_payouts(pilot))

    txn = _deposit(pilot, net, dt.date(2026, 10, 10))
    best = matching.suggestions(txn)[0]
    assert best.kind == "clearing" and best.extra["key"] == "stripe:po_123"
    item = next(i for i in todos.collect(pilot) if i.id == f"bank-match-{txn.pk}")
    assert item.post_data == {"clearing": "stripe:po_123"}

    board = login_board(client, pilot)
    resp = client.post(f"/money/bank/txn/{txn.pk}/confirm/", {"clearing": "stripe:po_123"})
    assert resp.status_code == 302, resp.content
    txn.refresh_from_db()
    p.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED and p.matched
    assert clearing_balance() == D("0.00")
    entry = p.match.entry
    assert {(ln.account.code, ln.debit, ln.credit) for ln in entry.lines.all()} == {
        ("1000", net, D("0.00")),
        ("1060", D("0.00"), net),
    }
    # Undo puts the payout back up for matching.
    matching.undo(txn, board)
    p.refresh_from_db()
    assert not p.matched and clearing_balance() == net


def test_payout_not_suggested_for_other_amount_or_date(client, pilot):
    services.payout_paid(pilot, "po_9", D("100.00"), dt.date(2026, 10, 1))
    assert not [
        s
        for s in matching.suggestions(_deposit(pilot, D("99.00"), dt.date(2026, 10, 1)))
        if s.kind == "clearing"
    ]
    assert not [
        s
        for s in matching.suggestions(_deposit(pilot, D("100.00"), dt.date(2026, 10, 20)))
        if s.kind == "clearing"
    ]


def test_webhook_body_is_json(client, pilot):
    assert json.loads(fake.event("payout.paid", {"id": "po"}))["type"] == "payout.paid"
