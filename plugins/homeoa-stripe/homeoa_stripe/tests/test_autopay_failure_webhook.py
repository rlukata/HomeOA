"""A Stripe failure webhook for an autopay debit fails the AutopayCharge and alerts."""

import logging
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import open_residents
from homeoa.ledger.models import JournalEntry
from homeoa.notify.models import Notification
from homeoa.payments import autopay
from homeoa.payments.models import AutopayCharge, AutopayRule, AutopayStatus, Receipt
from homeoa.registry.models import Person, Unit
from homeoa_stripe.models import IntentStatus, StripeIntent

from .test_pay_and_bank_link import NOV10, OCT7, OCT26, post_nov
from .test_stripe import chargeable_mandate, pay
from .test_stripe import post as _post

User = get_user_model()


_capture = None


@pytest.fixture(autouse=True)
def run_on_commit(django_capture_on_commit_callbacks):
    """Alerts are sent on commit; run them after each webhook in these tests."""
    global _capture
    _capture = django_capture_on_commit_callbacks
    yield
    _capture = None


def post(*args, **kwargs):
    with _capture(execute=True):
        return _post(*args, **kwargs)


@pytest.fixture
def residents(pilot):
    open_residents(pilot)
    return pilot


@pytest.fixture
def charge_a(residents):
    """Repro: enroll A, run 2026-10-26 (notice) and 2026-11-10 (debit)."""
    unit = Unit.objects.get(number="A")
    from homeoa.registry.services import current_owners

    autopay.enroll(
        unit,
        current_owners(unit)[0],
        rule=AutopayRule.BALANCE,
        fixed_amount=None,
        mandate_ref=str(chargeable_mandate(unit).pk),
        as_of=OCT7,
    )
    autopay.run(residents, OCT26)
    post_nov(residents)
    autopay.run(residents, NOV10)
    charge = AutopayCharge.objects.get(unit=unit)
    assert charge.status == AutopayStatus.PENDING
    return charge


def _intent(charge):
    return StripeIntent.objects.get(intent_id=charge.reference)


def _counts():
    return (
        Notification.objects.filter(type_key=autopay.OWNER_TYPE).count(),
        Notification.objects.filter(type_key=autopay.BOARD_TYPE).count(),
    )


def test_failed_while_pending(client, residents, charge_a):
    resp = post(client, residents, _intent(charge_a), "payment_intent.payment_failed")
    assert resp.status_code == 200
    charge_a.refresh_from_db()
    assert charge_a.status == AutopayStatus.FAILED and charge_a.failed_at
    owner, board = _counts()
    assert owner >= 1 and board >= 1
    assert autopay.todo_items(residents)
    assert not Receipt.objects.filter(reference=charge_a.reference).exists()


def test_duplicate_webhook_is_idempotent(client, residents, charge_a):
    intent = _intent(charge_a)
    post(client, residents, intent, "payment_intent.payment_failed", event_id="evt_dup")
    before = (_counts(), JournalEntry.objects.count())
    post(client, residents, intent, "payment_intent.payment_failed", event_id="evt_dup")
    # A different event id for the same failed intent changes nothing either.
    post(client, residents, intent, "payment_intent.payment_failed", event_id="evt_dup2")
    assert (_counts(), JournalEntry.objects.count()) == before
    charge_a.refresh_from_db()
    assert charge_a.status == AutopayStatus.FAILED


def _charge_failed(client, assoc, intent, event_id):
    """``charge.failed`` carries the intent id in ``payment_intent``."""
    from homeoa_stripe import fake
    from homeoa_stripe.tests.test_stripe import SECRET
    from homeoa_stripe.webhook import SIGNATURE_HEADER

    body = fake.event(
        "charge.failed",
        {"id": "ch_x", "object": "charge", "payment_intent": intent.intent_id},
        event_id=event_id,
    )
    with _capture(execute=True):
        return client.post(
            f"/hooks/stripe/{assoc.pk}/",
            body,
            content_type="application/json",
            headers={SIGNATURE_HEADER: fake.sign(body, SECRET)},
        )


def test_failed_after_success_goes_through_return_path(client, residents, charge_a):
    intent = _intent(charge_a)
    post(client, residents, intent, "payment_intent.succeeded", event_id="evt_ok")
    intent.refresh_from_db()
    receipt = intent.receipt
    assert receipt is not None
    entries = JournalEntry.objects.count()
    post(client, residents, intent, "payment_intent.payment_failed", event_id="evt_ret")
    intent.refresh_from_db()
    receipt.refresh_from_db()
    assert intent.status == IntentStatus.FAILED and intent.returned_at
    after_return = JournalEntry.objects.count()
    assert after_return > entries  # the void, posted once through payments services
    charge_a.refresh_from_db()
    assert charge_a.status == AutopayStatus.FAILED
    counts = _counts()
    assert counts[0] >= 1 and counts[1] >= 1
    # A later charge.failed for the same intent posts nothing more.
    _charge_failed(client, residents, intent, "evt_ret2")
    assert JournalEntry.objects.count() == after_return
    assert _counts() == counts


def test_unknown_reference_ignored_and_logged(client, residents, charge_a, caplog):
    from homeoa_stripe import fake
    from homeoa_stripe.tests.test_stripe import SECRET
    from homeoa_stripe.webhook import SIGNATURE_HEADER

    body = fake.event("payment_intent.payment_failed", {"id": "pi_nope"})
    with caplog.at_level(logging.INFO, logger="homeoa_stripe"):
        resp = client.post(
            f"/hooks/stripe/{residents.pk}/",
            body,
            content_type="application/json",
            headers={SIGNATURE_HEADER: fake.sign(body, SECRET)},
        )
    assert resp.status_code == 200
    assert "pi_nope" in caplog.text
    charge_a.refresh_from_db()
    assert charge_a.status == AutopayStatus.PENDING
    assert _counts() == (0, 0)


def test_board_alert_reaches_board_user_without_person(client, residents, charge_a):
    """board_recipients covers board logins with no Person record."""
    treasurer = User.objects.get(email="treasurer@cedarrow.example")
    post(client, residents, _intent(charge_a), "payment_intent.payment_failed")
    assert Notification.objects.filter(
        type_key=autopay.BOARD_TYPE, recipient__in=Person.objects.filter(user=treasurer)
    ).exists()


def test_non_autopay_intent_unchanged(client, residents, charge_a):
    unit_c = Unit.objects.get(number="C")
    intent = pay(unit_c, Decimal("10.00"))
    post(client, residents, intent, "payment_intent.payment_failed")
    intent.refresh_from_db()
    assert intent.status == IntentStatus.FAILED
    assert _counts() == (0, 0)
    charge_a.refresh_from_db()
    assert charge_a.status == AutopayStatus.PENDING


def test_notifier_error_never_blocks_the_failure(client, residents, charge_a, monkeypatch, caplog):
    """A notification never rolls back or blocks the charge state change."""
    from homeoa.audit.models import AuditEvent
    from homeoa.notify import services as notify_services

    def boom(*args, **kwargs):
        raise RuntimeError("mail is down")

    monkeypatch.setattr(notify_services, "notify", boom)
    with caplog.at_level(logging.ERROR, logger="homeoa.payments.autopay"):
        resp = post(client, residents, _intent(charge_a), "payment_intent.payment_failed")
    assert resp.status_code == 200
    charge_a.refresh_from_db()
    assert charge_a.status == AutopayStatus.FAILED
    assert "Autopay failure notice" in caplog.text
    assert AuditEvent.objects.filter(action="payments.autopay_notify_failed").count() == 2
