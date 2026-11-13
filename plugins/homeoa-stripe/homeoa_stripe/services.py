"""Stripe ACH: create intents and turn webhook outcomes into ledger postings.

``create_payment(unit, amount, mandate)`` asks Stripe to debit the payer's bank account and
returns a ``StripeIntent`` (status ``processing``, or ``failed`` if Stripe refused it). It
writes nothing to the ledger: money is recorded only when ``payment_intent.succeeded``
arrives. Permission checks belong to the caller (pay now / autopay). ``mandate`` must
be a chargeable :class:`StripeMandate` for the unit (verified, with Stripe's customer,
payment method and mandate ids); there is no offline-acceptance fallback.

``intent_succeeded(association, intent_id, fee)`` records the receipt (method ``ach``)
through ``payments.services.record_receipt`` exactly once per intent (the webhook view also
deduplicates on the event id), in the ownership period the intent was created for,
deposited to the configured deposit account (default 1060 Stripe clearing) in the operating
fund. ``fee`` is the actual fee from the balance transaction; ``intent.fee`` stays
the estimate. Absorbed (the default), the whole actual fee is posted Dr fee expense (5450)
/ Cr clearing. Passed through, the owner paid the estimate, so only the difference
(actual - estimate, either sign) is booked to 5450. Either way 1060 holds exactly the net
amount Stripe owes the association and never drifts.

Fee pass-through needs BOTH the rule ``payments.fee_pass_through_allowed`` resolving True
(WA pack: True, attorney review, a free method must exist) AND the board opting in
(plugin setting
``fee_mode=pass_through``). Otherwise the fee is absorbed.

Returns: ``payment_intent.payment_failed`` (or ``charge.failed``) after success is an ACH
return. The receipt is voided through ``payments.services.void_receipt`` (restoring the
balance), the intent is marked failed and returned, and a board to-do shows until
acknowledged. The processing fee stays expensed (Stripe keeps it).

Payouts: ``payout.paid`` records a :class:`StripePayout`. A banking suggester
(``payout_suggestions``, registered in ``apps.ready``) proposes the matching operating
deposit. Confirming posts Dr 1000 cash / Cr 1060 clearing via
``banking.matching.confirm_clearing``.

HomeOA never initiates payouts or refunds through Stripe.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.core.context import association_today
from homeoa.ledger.services import Line, post_entry
from homeoa.payments import services as payments
from homeoa.payments.models import Method
from homeoa.plugins.services import installation_for
from homeoa.registry.services import ownership_period_on

from . import fake
from .client import get_client
from .models import (
    FeeMode,
    IntentStatus,
    PayoutStatus,
    StripeIntent,
    StripeMandate,
    StripePayout,
)

logger = logging.getLogger(__name__)

PLUGIN_ID = "stripe"
OPERATING = "operating"
PAYOUT_WINDOW_DAYS = 5


def _settings(association) -> dict:
    inst = installation_for(association, PLUGIN_ID)
    return dict(inst.settings or {}) if inst else {}


def pass_through_allowed(association) -> bool:
    from homeoa import rules
    from homeoa.core.models import Association

    fresh = Association.objects.get(pk=association.pk)  # overrides live in settings JSON
    return rules.value(fresh, "payments.fee_pass_through_allowed") is True


def fee_mode(association) -> str:
    """Pass-through only when the rule allows it AND the board opted in; else absorb."""
    opted_in = _settings(association).get("fee_mode") == FeeMode.PASS_THROUGH
    if opted_in and pass_through_allowed(association):
        return FeeMode.PASS_THROUGH
    return FeeMode.ABSORB


def create_payment(
    unit,
    amount: Decimal,
    mandate: StripeMandate,
    *,
    client=None,
    ownership_period=None,
    idempotency_key: str = "",
) -> StripeIntent:
    """Debit the owner's bank account under a stored, verified mandate.

    Refused (``PaymentError``, nothing stored) without a chargeable :class:`StripeMandate`
    for this unit: every debit references the mandate the owner accepted online. A
    repeated ``idempotency_key`` returns the first intent instead of debiting again."""
    amount = payments.check_amount(amount)
    association = unit.association
    if not isinstance(mandate, StripeMandate):
        raise payments.PaymentError(_("Choose a verified bank account to pay from."))
    if mandate.association_id != association.pk or mandate.unit_id != unit.pk:
        raise payments.PaymentError(_("That bank authorization is for another unit."))
    if mandate.revoked_at is not None:
        raise payments.PaymentError(_("That bank authorization was revoked."))
    if not mandate.chargeable:
        raise payments.PaymentError(_("That bank account is not verified yet."))
    if idempotency_key:
        existing = StripeIntent.all_associations.filter(
            association=association, unit=unit, idempotency_key=idempotency_key
        ).first()
        if existing is not None:
            return existing
    mode = fee_mode(association)
    op = ownership_period or ownership_period_on(unit, association_today(association))
    if op is None:
        raise payments.PaymentError(_("Unit %(u)s has no owner of record.") % {"u": unit})
    fee = fake.ach_fee(amount)
    charged = amount + fee if mode == FeeMode.PASS_THROUGH else amount
    client = client or get_client(installation_for(association, PLUGIN_ID))
    try:
        data = client.create_payment_intent(
            amount=fake.to_cents(charged),
            metadata={"association_id": association.pk, "unit": unit.number},
            mandate=mandate.stripe_mandate_id,
            customer=mandate.stripe_customer_id,
            payment_method=mandate.stripe_payment_method_id,
            idempotency_key=idempotency_key and f"homeoa-pi-{association.pk}-{idempotency_key}",
        )
        intent_id, status, message = data["id"], IntentStatus.PROCESSING, ""
        if data.get("status") in ("requires_payment_method", "canceled"):
            status = IntentStatus.FAILED
            message = str((data.get("last_payment_error") or {}).get("message", ""))[:500]
    except Exception as exc:
        intent_id = "failed_" + secrets.token_hex(8)
        status, message = IntentStatus.FAILED, str(exc)[:500]
    intent = StripeIntent.all_associations.create(
        association=association,
        intent_id=intent_id,
        unit=unit,
        ownership_period=op,
        amount=amount,
        fee=fee,
        charged=charged,
        fee_mode=mode,
        status=status,
        mandate=mandate.stripe_mandate_id[:64],
        mandate_record=mandate,
        message=message,
        idempotency_key=idempotency_key[:100],
    )
    audit.record("stripe.intent_created", intent, association=association)
    return intent


def intent_succeeded(association, intent_id: str, fee: Decimal | None = None) -> StripeIntent:
    with transaction.atomic():
        intent = (
            StripeIntent.all_associations.select_for_update()
            .filter(association=association, intent_id=intent_id)
            .first()
        )
        if intent is None or intent.receipt_id is not None:
            return intent  # unknown or already recorded: idempotent no-op
        if intent.status != IntentStatus.PROCESSING:
            return intent  # failed before success: nothing to record
        conf = _settings(association)
        deposit = conf.get("deposit_account") or "1060"
        today = association_today(association)
        receipt = payments.record_receipt(
            unit=intent.unit,
            amount=intent.amount,
            received_date=today,
            method=Method.ACH,
            deposit_account=deposit,
            fund=OPERATING,
            payer=_("ACH via Stripe"),
            reference=intent.intent_id,
            ownership_period=intent.ownership_period,
            source="stripe",
        )
        intent.receipt = receipt
        intent.status = IntentStatus.SUCCEEDED
        actual = intent.fee if fee is None else fee
        intent.actual_fee = actual
        fields = ["receipt", "status", "actual_fee"]
        booked = actual if intent.fee_mode == FeeMode.ABSORB else actual - intent.fee
        if booked != 0:
            fee_account = conf.get("fee_account") or "5450"
            if intent.fee_mode == FeeMode.ABSORB:
                memo = _("ACH processing fee · Unit %(u)s · %(ref)s")
            else:
                memo = _("ACH fee difference (actual vs estimate) · Unit %(u)s · %(ref)s")
            memo = memo % {"u": intent.unit.number, "ref": intent.intent_id}
            # Positive: Dr fee / Cr clearing. Negative (Stripe took less): the reverse.
            dr, cr = (fee_account, deposit) if booked > 0 else (deposit, fee_account)
            intent.fee_entry = post_entry(
                [
                    Line(account=dr, fund=OPERATING, debit=abs(booked), memo=memo[:200]),
                    Line(account=cr, fund=OPERATING, credit=abs(booked), memo=memo[:200]),
                ],
                date=today,
                memo=memo,
                source_type="stripe.fee",
                source_id=intent.pk,
                association=association,
            )
            fields.append("fee_entry")
        intent.save(update_fields=fields)
        audit.record("stripe.intent_succeeded", intent, association=association)
    return intent


def intent_failed(association, intent_id: str, message: str = "") -> StripeIntent | None:
    """A failed debit. Before success: mark it failed. After success (an ACH return): void
    the receipt through payments services (restoring the balance) and raise a board to-do."""
    with transaction.atomic():
        intent = (
            StripeIntent.all_associations.select_for_update()
            .filter(association=association, intent_id=intent_id)
            .first()
        )
        if intent is None:
            logger.info(
                "Stripe failure for unknown intent %r (association %s) ignored",
                intent_id,
                association.pk,
            )
            return None
        if intent.status == IntentStatus.PROCESSING:
            intent.status = IntentStatus.FAILED
            intent.message = message[:500]
            intent.save(update_fields=["status", "message"])
            _autopay_failed(association, intent)
            return intent
        if intent.status != IntentStatus.SUCCEEDED or intent.receipt_id is None:
            return intent  # already failed or returned: idempotent
        reason = _("ACH payment returned by the bank")
        if message:
            reason = f"{reason}: {message}"
        payments.void_receipt(
            intent.receipt, reason=reason[:500], date=association_today(association)
        )
        intent.status = IntentStatus.FAILED
        intent.message = message[:500]
        intent.returned_at = timezone.now()
        intent.save(update_fields=["status", "message", "returned_at"])
        audit.record("stripe.intent_returned", intent, association=association, message=message)
        _autopay_failed(association, intent)
    return intent


def _autopay_failed(association, intent: StripeIntent) -> None:
    """Tell core autopay a debit it started failed (it notifies the owner and the board)."""
    from homeoa.payments import autopay

    autopay.reference_failed(association, intent.intent_id, intent.message)


def acknowledge_return(intent: StripeIntent, *, user=None) -> StripeIntent:
    if intent.returned_at is not None and intent.return_acknowledged_at is None:
        intent.return_acknowledged_at = timezone.now()
        intent.save(update_fields=["return_acknowledged_at"])
        audit.record(
            "stripe.return_acknowledged",
            intent,
            association=intent.association,
            actor=user if getattr(user, "is_authenticated", False) else None,
        )
    return intent


# ------------------------------------------------------------------ payouts


def payout_paid(association, payout_id: str, amount: Decimal, arrival_date) -> StripePayout:
    """Record a payout Stripe sent (idempotent per payout id). Posts nothing: the money
    moves from 1060 to cash when the board confirms the bank deposit match."""
    payout, created = StripePayout.all_associations.get_or_create(
        association=association,
        payout_id=payout_id,
        defaults={"amount": amount, "arrival_date": arrival_date},
    )
    if created:
        audit.record("stripe.payout_paid", payout, association=association)
    return payout


def payout_failed(association, payout_id: str) -> StripePayout | None:
    payout = StripePayout.all_associations.filter(
        association=association, payout_id=payout_id
    ).first()
    if payout is not None and not payout.matched:
        payout.status = PayoutStatus.FAILED
        payout.save(update_fields=["status"])
    return payout


def open_payouts(association) -> list[StripePayout]:
    qs = StripePayout.all_associations.filter(
        association=association, status=PayoutStatus.PAID
    ).select_related("match")
    return [p for p in qs if not p.matched]


def payout_suggestions(txn) -> list:
    """Banking suggester: an operating deposit equal to an unmatched payout near its date."""
    from homeoa.banking.matching import Suggestion
    from homeoa.ledger.models import Account

    association = txn.association
    if txn.amount <= 0 or txn.bank_account.fund.code != OPERATING:
        return []
    inst = installation_for(association, PLUGIN_ID)
    if inst is None or not inst.enabled:
        return []
    deposit = (inst.settings or {}).get("deposit_account") or "1060"
    clearing = Account.all_associations.filter(association=association, code=deposit).first()
    if clearing is None:
        return []
    out = []
    for payout in open_payouts(association):
        if payout.amount != txn.amount:
            continue
        if abs((payout.arrival_date - txn.date).days) > PAYOUT_WINDOW_DAYS:
            continue

        def on_confirm(match, payout=payout):
            payout.match = match
            payout.save(update_fields=["match"])
            audit.record("stripe.payout_matched", payout, association=association)

        out.append(
            Suggestion(
                kind="clearing",
                score=95,
                reason=_("Stripe payout %(id)s") % {"id": payout.payout_id},
                amount=txn.amount,
                account=clearing,
                extra={"key": f"stripe:{payout.payout_id}", "on_confirm": on_confirm},
            )
        )
    return out


# ------------------------------------------------------------------ to-dos


def todo_items(association) -> list[dict]:
    """Board to-dos: returned ACH payments not yet acknowledged."""
    from django.urls import reverse

    items = []
    returned = StripeIntent.all_associations.filter(
        association=association, returned_at__isnull=False, return_acknowledged_at__isnull=True
    ).select_related("unit", "receipt")
    for intent in returned:
        when = intent.receipt.received_date if intent.receipt_id else intent.returned_at.date()
        items.append(
            {
                "id": f"stripe-return-{intent.pk}",
                "kind": "money",
                "title": _("ACH payment of %(amount)s from unit %(unit)s on %(date)s failed")
                % {
                    "amount": f"${intent.amount:,.2f}",
                    "unit": intent.unit.number,
                    "date": date_format(when, "M j"),
                },
                "sub": intent.message or _("The bank returned the payment; it was voided."),
                "due": association_today(association),
                "url": reverse("payments:detail", args=[intent.receipt_id])
                if intent.receipt_id
                else "",
                "action": _("Got it"),
                "post_url": reverse("homeoa_stripe:ack_return", args=[intent.pk]),
                "post_data": {},
            }
        )
    return items
