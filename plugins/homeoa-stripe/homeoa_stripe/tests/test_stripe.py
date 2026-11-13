import datetime as dt
import io
import json
from decimal import Decimal

import pytest
from django.core.management import call_command

from homeoa import providers
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.ledger.models import JournalLine
from homeoa.ledger.services import trial_balance
from homeoa.payments.models import Receipt
from homeoa.payments.services import open_charges
from homeoa.plugins import services as plugins
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import ownership_period_on, transfer_ownership
from homeoa_stripe import fake
from homeoa_stripe.models import IntentStatus
from homeoa_stripe.provider import StripeACH
from homeoa_stripe.services import create_payment
from homeoa_stripe.webhook import SIGNATURE_HEADER

SECRET = demo.STRIPE_FAKE_WEBHOOK_SECRET
DUES_C = Decimal("396.63")


@pytest.fixture(autouse=True)
def fake_state(settings, tmp_path):
    settings.HOMEOA_STRIPE_FAKE_STATE = str(tmp_path / "stripe.json")


@pytest.fixture
def pilot(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    a = demo.seed()
    with association_context(a):
        oct1 = dt.date(2026, 10, 1)
        if not Charge.objects.filter(period=oct1).exists():
            generate_assessments(AssessmentSchedule.objects.get(), oct1, source="test")
        yield a


@pytest.fixture
def unit_c(pilot):
    return Unit.objects.get(number="C")


def post(client, assoc, intent, event_type="payment_intent.succeeded", event_id="", **kw):
    body = fake.event(
        event_type,
        {"id": intent.intent_id, "object": "payment_intent"},
        fee=fake.to_cents(intent.fee),
        event_id=event_id,
    )
    sig = kw.get("signature") or fake.sign(body, kw.get("secret", SECRET))
    return client.post(
        f"/hooks/stripe/{assoc.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: sig},
    )


def chargeable_mandate(unit):
    """The unit owner's verified bank mandate (the demo seed links A and C; others here)."""
    from django.utils import timezone

    from homeoa.registry.services import current_owners
    from homeoa_stripe import mandates
    from homeoa_stripe.models import StripeMandate

    found = [m for m in StripeMandate.all_associations.filter(unit=unit) if m.chargeable]
    if found:
        return found[0]
    owner = current_owners(unit)[0]
    tag = f"t{unit.pk}"
    return mandates.record_mandate(
        unit,
        owner,
        "6789",
        ip_address="203.0.113.5",
        user_agent="pytest",
        stripe_mandate_id=f"mandate_{tag}",
        stripe_customer_id=f"cus_{tag}",
        stripe_payment_method_id=f"pm_{tag}",
        verified_at=timezone.now(),
    )


def pay(unit, amount, **kw):
    return create_payment(unit, amount, chargeable_mandate(unit), **kw)


def c_balance(unit):
    return sum((c.open_balance for c in open_charges(unit)), Decimal("0.00"))


def test_provider_resolves_when_enabled(pilot):
    assert isinstance(providers.get_provider("PaymentProvider", pilot), StripeACH)
    plugins.save_installation(pilot, "stripe", enabled=False, data={})
    assert not isinstance(providers.get_provider("PaymentProvider", pilot), StripeACH)


def test_create_payment_is_pending_and_posts_nothing(unit_c):
    lines = JournalLine.objects.count()
    intent = pay(unit_c, DUES_C)
    assert intent.status == IntentStatus.PROCESSING and intent.intent_id.startswith("pi_")
    assert intent.amount == intent.charged == DUES_C
    assert intent.fee == Decimal("3.17")  # 0.8%, half-up
    assert JournalLine.objects.count() == lines


def test_fail_next(unit_c):
    fake.fail_next()
    assert pay(unit_c, DUES_C).status == IntentStatus.FAILED
    assert pay(unit_c, DUES_C).status == IntentStatus.PROCESSING


def test_collect_via_provider_interface(unit_c, pilot):
    from homeoa.providers.payments import PaymentRequest, PaymentStatus

    result = providers.get_provider("PaymentProvider", pilot).collect(
        PaymentRequest(
            association_id=pilot.pk,
            unit_id=unit_c.pk,
            amount=DUES_C,
            mandate_ref=str(chargeable_mandate(unit_c).pk),
        )
    )
    assert result.status == PaymentStatus.PENDING and result.reference.startswith("pi_")


def test_succeeded_records_receipt_and_absorbs_fee(client, pilot, unit_c):
    before = c_balance(unit_c)
    intent = pay(unit_c, DUES_C)
    assert post(client, pilot, intent).status_code == 200
    intent.refresh_from_db()
    assert intent.status == IntentStatus.SUCCEEDED
    r = intent.receipt
    assert r.amount == DUES_C and r.reference == intent.intent_id and r.source == "stripe"
    assert r.deposit_account.code == "1060" and r.fund.code == "operating"
    assert c_balance(unit_c) == before - DUES_C
    fee_lines = intent.fee_entry.lines.all()
    assert {(ln.account.code, ln.debit, ln.credit) for ln in fee_lines} == {
        ("5450", Decimal("3.17"), Decimal("0.00")),
        ("1060", Decimal("0.00"), Decimal("3.17")),
    }
    tb = trial_balance()
    assert tb.total_debit == tb.total_credit


def test_receipt_audited_under_webhook_label(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    post(client, pilot, intent, event_id="evt_audit")
    receipt = Receipt.objects.get(reference=intent.intent_id)
    events = AuditEvent.objects.filter(object_type="payments.receipt", object_id=str(receipt.pk))
    assert events.exists() and all(e.actor_label == "webhook:evt_audit" for e in events)


def test_replay_and_second_event_ignored(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    assert post(client, pilot, intent, event_id="evt_1").status_code == 200
    again = post(client, pilot, intent, event_id="evt_1")
    assert again.status_code == 200 and again.content == b"duplicate"
    # A different event id for the same intent (Stripe retries can re-id) is a no-op too.
    assert post(client, pilot, intent, event_id="evt_2").status_code == 200
    assert Receipt.objects.filter(reference=intent.intent_id).count() == 1
    assert JournalLine.objects.filter(entry__source_type="stripe.fee").count() == 2


def test_bad_signature_rejected(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    assert post(client, pilot, intent, secret="whsec_wrong").status_code == 400
    assert post(client, pilot, intent, signature="t=1,v1=bad").status_code == 400
    stale = fake.event("payment_intent.succeeded", {"id": intent.intent_id})
    resp = client.post(
        f"/hooks/stripe/{pilot.pk}/",
        stale,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(stale, SECRET, timestamp=1)},
    )
    assert resp.status_code == 400
    assert not Receipt.objects.filter(reference=intent.intent_id).exists()


def test_fee_pass_through(client, pilot, unit_c):
    plugins.save_installation(
        pilot, "stripe", enabled=True, data={"webhook_secret": SECRET, "fee_mode": "pass_through"}
    )
    # Board opted in, but the association turned the rule off -> still absorbed.
    from homeoa import rules

    rules.set_override(pilot, "payments.fee_pass_through_allowed", False, reason="test")
    assert pay(unit_c, DUES_C).charged == DUES_C
    # WA pack allows it once a free method exists; board opt-in passes it through.
    rules.clear_override(pilot, "payments.fee_pass_through_allowed", reason="test")
    intent = pay(unit_c, DUES_C)
    assert intent.charged == DUES_C + Decimal("3.17")
    post(client, pilot, intent)
    intent.refresh_from_db()
    assert intent.receipt.amount == DUES_C and intent.fee_entry is None


def test_failed_before_success(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)
    post(client, pilot, intent, event_type="payment_intent.payment_failed")
    intent.refresh_from_db()
    assert intent.status == IntentStatus.FAILED and intent.receipt is None


def test_receipt_stays_in_created_period(client, pilot, unit_c):
    seller = ownership_period_on(unit_c, dt.date(2026, 10, 7))
    intent = pay(unit_c, DUES_C)
    buyer = Person.objects.create(association=pilot, full_name="New Buyer")
    transfer_ownership(unit_c, [buyer], dt.date(2026, 10, 7))
    post(client, pilot, intent)
    intent.refresh_from_db()
    assert intent.receipt.ownership_period_id == seller.pk


def test_stripe_fake_command(client, pilot, unit_c):
    intent = pay(unit_c, DUES_C)

    def run(*args):
        out = io.StringIO()
        call_command("stripe_fake", *args, stdout=out)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    last = run("last-intent")
    assert last == {"id": intent.intent_id, "amount": "396.63", "status": "processing"}
    hook = run("webhook", "payment_intent.succeeded", intent.intent_id)
    assert hook["association_id"] == pilot.pk
    resp = client.post(
        f"/hooks/stripe/{pilot.pk}/",
        hook["body"],
        content_type="application/json",
        headers={SIGNATURE_HEADER: hook["signature"]},
    )
    assert resp.status_code == 200
    assert run("last-intent")["status"] == "succeeded"
    assert run("fail-next") == {"fail_next": True}
    assert pay(unit_c, DUES_C).status == IntentStatus.FAILED


def test_no_payout_api():
    assert not any("payout" in name.lower() for name in dir(fake.FakeStripe))


def test_clearing_holds_net_payout(client, pilot):
    from django.db.models import Sum

    total = Decimal("0.00")
    for number in ("A", "B", "C"):
        unit = Unit.objects.get(number=number)
        for amount in (Decimal("100.00"), Decimal("900.00")):  # fee 0.80 and capped 5.00
            intent = pay(unit, amount)
            post(client, pilot, intent)
            total += amount - fake.ach_fee(amount)
    agg = JournalLine.objects.filter(account__code="1060").aggregate(
        d=Sum("debit"), c=Sum("credit")
    )
    assert agg["d"] - agg["c"] == total == Decimal("2982.60")


def test_fee_mode_hidden_from_board_form(pilot):
    from homeoa.plugins.forms import PluginSettingsForm
    from homeoa.plugins.services import installation_for
    from homeoa_stripe.manifest import manifest

    plugins.save_installation(
        pilot, "stripe", enabled=True, data={"webhook_secret": SECRET, "fee_mode": "pass_through"}
    )
    form = PluginSettingsForm(manifest, installation_for(pilot, "stripe"))
    assert "fee_mode" not in form.fields
    assert form.hidden_values == {"fee_mode": "pass_through"}
