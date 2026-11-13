"""Receipts, the allocation engine, credits and refunds (FIN-05, FIN-11).

The only write path for :class:`~homeoa.payments.models.Receipt`,
:class:`~homeoa.payments.models.Allocation` and :class:`~homeoa.payments.models.Refund`.
Every ledger change goes through ``ledger.services.post_entry`` / ``reverse_entry``; every
write is audited (models registered with ``audit`` plus ``payments.*`` events).

Public API (also for deposit matching)
---------------------------------------------
``record_receipt(*, unit, amount, received_date, method, deposit_account=None, fund=None,
payer="", reference="", memo="", ownership_period=None, posted_by=None, source="manual")
-> Receipt``
    Records money that already arrived (HomeOA never initiates payments). One entry:
    Dr deposit account / Cr 1100 per allocated charge / Cr 2100 for any excess, all tagged
    to the unit and the ownership period (the owner of record on ``received_date`` unless
    given). ``deposit_account`` / ``fund`` are ``Account`` / ``Fund`` instances or codes;
    default 1000 / operating. Bank matching passes ``bank_account.gl_account`` and
    ``bank_account.fund`` and ``source="bank"``.

``void_receipt(receipt, *, reason, posted_by=None, date=None) -> Receipt``
    One entry undoes the receipt as it stands now: Cr cash for the full amount, Dr 1100 for
    each active allocation (including credit applications it funded), Dr 2100 for its
    unapplied credit. The charges reopen, then any other credit the unit holds re-applies.

``record_refund(receipt, *, amount, date, method, reason, reference="", posted_by=None)``
    Records a refund the board already issued outside HomeOA: Dr 2100 / Cr cash,
    limited to the receipt's unapplied credit.

``apply_credit(unit, *, date, ownership_period=None, posted_by=None) -> list[Allocation]``
    Applies unapplied credit (receipts and credit memos, oldest first) to open charges in
    the configured order: Dr 2100 / Cr 1100 per source. Runs automatically whenever a
    charge is created (``post_save`` on ``billing.Charge``), so the next charge consumes a
    prepayment. ``reallocate`` is an alias for callers after charges change.

``allocation_plan(association, charges, amount) -> list[(Charge, Decimal)]``
    Pure: how ``amount`` would be split over ``charges`` (no writes). ``order_charges``
    sorts charges by the rule pack's ``payments.allocation_order`` (buckets assessment,
    late_fee, interest, other) and ``payments.oldest_first``; ``mode="oldest"`` sorts
    strictly by date across types.

Queries: ``unapplied(receipt)``, ``unit_credit(unit_id, ownership_period_id=None)``,
``open_charges(unit, ownership_period=None)``. Errors raise :class:`PaymentError`.

Allocation stays within one ownership period: a receipt or credit pays only charges billed
to the owner of record it is tagged to (seller and buyer balances stay apart).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils.translation import gettext as _

from homeoa import rules
from homeoa.audit.services import record
from homeoa.billing.models import Adjustment, AdjustmentKind, Charge, ChargeKind
from homeoa.core.context import association_today
from homeoa.core.fields import ZERO
from homeoa.ledger.chart import interfund_codes
from homeoa.ledger.models import Account, Fund, JournalLine
from homeoa.ledger.services import LedgerError, Line, post_entry
from homeoa.registry.models import OwnershipPeriod, Unit
from homeoa.registry.services import ownership_period_on

from .models import Allocation, Receipt, ReceiptStatus, Refund

CENT = Decimal("0.01")
RECEIVABLE = "1100"
PREPAID = "2100"
CASH = "1000"
DUE_FROM = "1900"  # interfund: due from other fund
DUE_TO = "2900"  # interfund: due to other fund


def _due_from(cash_fund) -> str:
    """Operating's due-from code for cash held in ``cash_fund`` (1920 for insurance)."""
    return interfund_codes(cash_fund, "operating")[0]


def _due_to(cash_fund) -> str:
    return interfund_codes(cash_fund, "operating")[1]


OPERATING = "operating"

BUCKETS = {
    ChargeKind.ASSESSMENT: "assessment",
    ChargeKind.LATE_FEE: "late_fee",
    ChargeKind.INTEREST: "interest",
}
# Adjustment kinds that are credit sources (apply to charges like a receipt).
CREDIT_KINDS = (AdjustmentKind.CREDIT_MEMO, AdjustmentKind.WRITE_OFF)


class PaymentError(ValueError):
    """The payment change was refused; nothing was written."""


def _actor(user):
    return user if getattr(user, "pk", None) else None


def check_amount(value) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PaymentError(_("The amount must be a Decimal."))
    if value != value.quantize(CENT):
        raise PaymentError(_("The amount can have at most two decimal places."))
    if value <= 0:
        raise PaymentError(_("The amount must be more than zero."))
    return value.quantize(CENT)


def _resolve(model, value, association, default):
    value = value if value is not None else default
    if isinstance(value, model):
        if value.association_id != association.pk:
            raise PaymentError(_("That account belongs to another association."))
        return value
    try:
        return model.all_associations.get(association=association, code=value)
    except model.DoesNotExist:
        raise PaymentError(_("Unknown account or fund %(c)s.") % {"c": value}) from None


def operating_fund(association) -> Fund:
    """The fund owners are billed in: receivables and prepaid credit live here."""
    return _resolve(Fund, None, association, OPERATING)


# ------------------------------------------------------------------ ordering


def bucket(kind: str) -> str:
    return BUCKETS.get(kind, "other")


def order_charges(
    charges: Iterable[Charge],
    order: Iterable[str],
    *,
    oldest_first: bool = True,
    mode: str = "by_type",
) -> list[Charge]:
    """Charges in the order a payment pays them.

    ``mode="by_type"``: by the bucket's position in ``order`` (unknown buckets last), then
    by date (oldest first unless ``oldest_first`` is False). ``mode="oldest"``: strictly by
    date, the bucket order breaking ties.
    """
    order = list(order)
    rank = {b: i for i, b in enumerate(order)}

    def key(c):
        r = rank.get(bucket(c.kind), len(order))
        when = (c.charge_date.toordinal(), c.due_date.toordinal(), c.pk or 0)
        if mode == "oldest":
            return (when, r)
        if not oldest_first:
            when = tuple(-x for x in when)
        return (r, when)

    return sorted(charges, key=key)


def allocation_settings(association) -> tuple[tuple[str, ...], bool]:
    return (
        tuple(rules.payment_allocation_order(association)),
        bool(rules.value(association, "payments.oldest_first")),
    )


def allocation_plan(association, charges, amount: Decimal) -> list[tuple[Charge, Decimal]]:
    order, oldest = allocation_settings(association)
    # payments.allocation_mode (rule pack, unverified): "oldest_first" pays strictly by date.
    mode = rules.value(association, "payments.allocation_mode") or "by_type"
    mode = "oldest" if mode == "oldest_first" else "by_type"
    plan, left = [], amount
    for charge in order_charges(charges, order, oldest_first=oldest, mode=mode):
        if left <= 0:
            break
        take = min(left, charge.open_balance)
        if take > 0:
            plan.append((charge, take))
            left -= take
    return plan


# ------------------------------------------------------------------ queries


def settle_from_ledger(unit, ownership_period=None) -> Decimal:
    """Settle charges paid outside this engine (imported history), oldest first.

    For each ownership period, the open balances of the unit's charges should equal the
    net of its 1100 receivable lines. Imported payments credit 1100 with no allocation, so
    when the charges' open total is higher, the difference is taken off the oldest open
    charges (no ledger posting: the ledger already has it). Never raises open balances.
    Returns the total settled.
    """
    qs = JournalLine.all_associations.filter(
        association_id=unit.association_id, unit_id=unit.pk, account__code=RECEIVABLE
    )
    charges = Charge.all_associations.filter(
        association_id=unit.association_id, unit=unit, void_entry__isnull=True
    )
    if ownership_period is not None:
        charges = charges.filter(ownership_period=ownership_period)
    settled = ZERO
    for op_id in set(charges.values_list("ownership_period_id", flat=True)):
        agg = qs.filter(ownership_period_id=op_id).aggregate(d=Sum("debit"), c=Sum("credit"))
        target = max(ZERO, (agg["d"] or ZERO) - (agg["c"] or ZERO))
        rows = list(
            charges.filter(ownership_period_id=op_id, open_balance__gt=0)
            .select_for_update()
            .order_by("charge_date", "pk")
        )
        excess = sum((c.open_balance for c in rows), ZERO) - target
        for c in rows:
            if excess <= 0:
                break
            take = min(excess, c.open_balance)
            Charge.all_associations.filter(pk=c.pk).update(open_balance=F("open_balance") - take)
            excess -= take
            settled += take
    return settled


def open_charges(unit, ownership_period=None, *, lock=False):
    qs = Charge.all_associations.filter(
        association_id=unit.association_id,
        unit=unit,
        open_balance__gt=0,
        void_entry__isnull=True,
    )
    if ownership_period is not None:
        qs = qs.filter(ownership_period=ownership_period)
    if lock:
        qs = qs.select_for_update()
    return list(qs.order_by("charge_date", "pk"))


def _active(qs):
    return qs.filter(released_entry__isnull=True)


def _allocated(**source) -> Decimal:
    agg = _active(Allocation.all_associations.filter(**source)).aggregate(s=Sum("amount"))
    return agg["s"] or ZERO


def unapplied(receipt: Receipt) -> Decimal:
    """The part of a posted receipt not applied to charges or refunded (its credit)."""
    if receipt.status != ReceiptStatus.POSTED:
        return ZERO
    refunded = receipt.refunds.aggregate(s=Sum("amount"))["s"] or ZERO
    return receipt.amount - _allocated(receipt=receipt) - refunded


def adjustment_unapplied(adjustment: Adjustment) -> Decimal:
    if adjustment.kind != AdjustmentKind.CREDIT_MEMO or adjustment.entry_id is None:
        return ZERO
    return adjustment.amount - _allocated(adjustment=adjustment)


def unit_credit(unit_id: int, ownership_period_id: int | None = None, *, association) -> Decimal:
    """Unapplied credit (prepaid) of a unit, optionally for one ownership period."""
    total = ZERO
    for source in _credit_sources(unit_id, ownership_period_id, association):
        total += _source_unapplied(source)
    return total


def _credit_sources(unit_id, ownership_period_id, association):
    filt = {"association": association, "unit_id": unit_id}
    if ownership_period_id is not None:
        filt["ownership_period_id"] = ownership_period_id
    sources = list(Receipt.all_associations.filter(status=ReceiptStatus.POSTED, **filt))
    sources += list(
        Adjustment.all_associations.filter(
            kind=AdjustmentKind.CREDIT_MEMO, entry__isnull=False, **filt
        )
    )
    return sorted(sources, key=lambda s: (_source_date(s), s.created_at, s.pk))


def _source_date(source) -> dt.date:
    return source.received_date if isinstance(source, Receipt) else source.date


def _source_unapplied(source) -> Decimal:
    return unapplied(source) if isinstance(source, Receipt) else adjustment_unapplied(source)


def _source_kw(source) -> dict:
    return {"receipt": source} if isinstance(source, Receipt) else {"adjustment": source}


# ------------------------------------------------------------------ allocation core


def _lock_unit(unit) -> Unit:
    locked = Unit.all_associations.select_for_update().get(pk=unit.pk)
    settle_from_ledger(locked)
    return locked


def _create_allocations(source, plan, entry) -> list[Allocation]:
    out = []
    for charge, amount in plan:
        out.append(
            Allocation.all_associations.create(
                association_id=charge.association_id,
                charge=charge,
                amount=amount,
                entry=entry,
                **_source_kw(source),
            )
        )
        Charge.all_associations.filter(pk=charge.pk).update(open_balance=F("open_balance") - amount)
        charge.open_balance -= amount
    return out


def _release(allocations: Iterable[Allocation], released_entry) -> None:
    for alloc in allocations:
        Allocation.all_associations.filter(pk=alloc.pk).update(released_entry=released_entry)
        Charge.all_associations.filter(pk=alloc.charge_id).update(
            open_balance=F("open_balance") + alloc.amount
        )


def _receivable_lines(plan, op_id, fund, memo_prefix="") -> list[Line]:
    return [
        Line(
            account=RECEIVABLE,
            fund=fund,
            credit=amount,
            unit_id=charge.unit_id,
            ownership_period_id=op_id,
            memo=f"{memo_prefix}{charge.description}"[:200],
        )
        for charge, amount in plan
    ]


def post_credit_source(
    source,
    *,
    debit: Line,
    amount: Decimal,
    date: dt.date,
    memo: str,
    source_type: str,
    posted_by=None,
    first_charge: Charge | None = None,
    allow_excess: bool = True,
    extra_lines: Iterable[Line] = (),
):
    """Allocate ``amount`` from a saved receipt or adjustment and post its one entry.

    ``debit`` is the line that funds it (cash for a receipt, an income or expense account
    for an adjustment). Charges are paid in the configured order (``first_charge`` first);
    any excess goes to 2100 prepaid, or is refused when ``allow_excess`` is False.
    Returns ``(entry, allocations)``. Call inside a transaction holding the unit lock.
    """
    association = source.association
    op = source.ownership_period
    charges = open_charges(source.unit, op, lock=True)
    plan = []
    left = amount
    if first_charge is not None:
        first = next((c for c in charges if c.pk == first_charge.pk), None)
        if first is None:
            raise PaymentError(_("That charge has nothing left to pay."))
        take = min(left, first.open_balance)
        plan.append((first, take))
        left -= take
        charges = [c for c in charges if c.pk != first.pk]
    if left > 0:
        plan += allocation_plan(association, charges, left)
        left = amount - sum((a for _c, a in plan), ZERO)
    if left > 0 and not allow_excess:
        raise PaymentError(
            _("The amount is more than the open charges (%(open)s).")
            % {"open": f"{amount - left:,.2f}"}
        )
    lines = [debit, *extra_lines, *_receivable_lines(plan, op.pk, debit.fund)]
    if left > 0:
        lines.append(
            Line(
                account=PREPAID,
                fund=debit.fund,
                credit=left,
                unit_id=source.unit_id,
                ownership_period_id=op.pk,
                memo=_("Unapplied credit"),
            )
        )
    try:
        entry = post_entry(
            lines,
            date=date,
            memo=memo,
            source_type=source_type,
            source_id=source.pk,
            posted_by=_actor(posted_by),
            association=association,
        )
    except LedgerError as exc:
        raise PaymentError(str(exc)) from exc
    return entry, _create_allocations(source, plan, entry)


# ------------------------------------------------------------------ receipts


def record_receipt(
    *,
    unit: Unit,
    amount: Decimal,
    received_date: dt.date,
    method: str,
    deposit_account=None,
    fund=None,
    payer: str = "",
    reference: str = "",
    memo: str = "",
    ownership_period: OwnershipPeriod | None = None,
    posted_by=None,
    source: str = "manual",
) -> Receipt:
    """Record a receipt and apply it. See the module docstring."""
    amount = check_amount(amount)
    association = unit.association
    account = _resolve(Account, deposit_account, association, CASH)
    fund = _resolve(Fund, fund, association, OPERATING)
    with transaction.atomic():
        unit = _lock_unit(unit)
        op = ownership_period or ownership_period_on(unit, received_date)
        if op is None:
            raise PaymentError(_("Unit %(u)s has no owner of record on that date.") % {"u": unit})
        if op.unit_id != unit.pk:
            raise PaymentError(_("That ownership period belongs to another unit."))
        receipt = Receipt.all_associations.create(
            association=association,
            unit=unit,
            ownership_period=op,
            payer=payer,
            method=method,
            reference=reference,
            amount=amount,
            received_date=received_date,
            deposit_account=account,
            fund=fund,
            memo=memo,
            source=source,
            created_by=_actor(posted_by),
        )
        label = _("Payment · Unit %(u)s") % {"u": unit.number}
        if reference:
            label = f"{label} · {reference}"
        debit = Line(account=account, fund=fund, debit=amount, memo=label[:200])
        extra = []
        billing = operating_fund(association)
        if fund.pk != billing.pk:
            # Deposited into another fund's account (e.g. reserve): owners are billed in
            # operating, so the cash fund owes operating: Dr cash / Cr 2900 in the
            # deposit fund, Dr 1900 / Cr 1100 in operating (1920/2920 for insurance).
            due_from, due_to = interfund_codes(fund, billing)
            extra = [debit, Line(account=due_to, fund=fund, credit=amount, memo=label[:200])]
            debit = Line(account=due_from, fund=billing, debit=amount, memo=label[:200])
        entry, _allocs = post_credit_source(
            receipt,
            debit=debit,
            extra_lines=extra,
            amount=amount,
            date=received_date,
            memo=label,
            source_type="payments.receipt",
            posted_by=posted_by,
        )
        receipt.entry = entry
        receipt.save(update_fields=["entry"])
    return receipt


def void_receipt(receipt: Receipt, *, reason: str, posted_by=None, date=None) -> Receipt:
    """Reverse a receipt, its allocations and any credit applications it funded."""
    reason = (reason or "").strip()
    if not reason:
        raise PaymentError(_("Give a reason for voiding."))
    with transaction.atomic():
        _lock_unit(receipt.unit)
        receipt = Receipt.all_associations.select_for_update().get(pk=receipt.pk)
        if receipt.status == ReceiptStatus.VOID:
            raise PaymentError(_("This receipt is already void."))
        if receipt.refunds.exists():
            raise PaymentError(_("A refund was recorded against this receipt; it can't be voided."))
        date = date or association_today(receipt.association)
        actor = _actor(posted_by)
        active = list(_active(receipt.allocations.select_related("charge")))
        left = unapplied(receipt)
        tag = {"unit_id": receipt.unit_id, "ownership_period_id": receipt.ownership_period_id}
        billing = operating_fund(receipt.association)
        lines = [Line(account=receipt.deposit_account, fund=receipt.fund, credit=receipt.amount)]
        if receipt.fund_id != billing.pk:  # undo the interfund lines too
            lines += [
                Line(account=_due_to(receipt.fund), fund=receipt.fund, debit=receipt.amount),
                Line(account=_due_from(receipt.fund), fund=billing, credit=receipt.amount),
            ]
        lines += [
            Line(
                account=RECEIVABLE,
                fund=billing,
                debit=a.amount,
                memo=a.charge.description[:200],
                unit_id=a.charge.unit_id,
                ownership_period_id=a.charge.ownership_period_id,
            )
            for a in active
        ]
        if left > 0:
            lines.append(Line(account=PREPAID, fund=billing, debit=left, **tag))
        try:
            void_entry = post_entry(
                lines,
                date=date,
                memo=_("Void of payment %(n)s") % {"n": receipt.pk},
                source_type="payments.receipt_void",
                source_id=receipt.pk,
                posted_by=actor,
                association=receipt.association,
            )
        except LedgerError as exc:
            raise PaymentError(str(exc)) from exc
        _release(active, void_entry)
        receipt.status = ReceiptStatus.VOID
        receipt.void_entry = void_entry
        receipt.void_reason = reason
        receipt.save(update_fields=["status", "void_entry", "void_reason"])
        record("payments.receipt_void", receipt, actor=actor, message=reason)
        apply_credit(receipt.unit, date=date, posted_by=posted_by)
    return receipt


def record_refund(
    receipt: Receipt,
    *,
    amount: Decimal,
    date: dt.date,
    method: str,
    reason: str,
    reference: str = "",
    posted_by=None,
) -> Refund:
    """Record a refund already issued outside HomeOA. Never moves money."""
    amount = check_amount(amount)
    reason = (reason or "").strip()
    if not reason:
        raise PaymentError(_("Give a reason for the refund."))
    with transaction.atomic():
        _lock_unit(receipt.unit)
        receipt = Receipt.all_associations.select_for_update().get(pk=receipt.pk)
        available = unapplied(receipt)
        if amount > available:
            raise PaymentError(
                _("Only %(a)s of this payment is unapplied credit.") % {"a": f"{available:,.2f}"}
            )
        actor = _actor(posted_by)
        memo = _("Refund issued outside HomeOA · Unit %(u)s") % {"u": receipt.unit.number}
        billing = operating_fund(receipt.association)
        lines = [
            Line(
                account=PREPAID,
                fund=billing,
                debit=amount,
                unit_id=receipt.unit_id,
                ownership_period_id=receipt.ownership_period_id,
            ),
            Line(account=receipt.deposit_account, fund=receipt.fund, credit=amount),
        ]
        if receipt.fund_id != billing.pk:  # cash leaves the deposit fund (interfund)
            lines += [
                Line(account=_due_to(receipt.fund), fund=receipt.fund, debit=amount),
                Line(account=_due_from(receipt.fund), fund=billing, credit=amount),
            ]
        try:
            entry = post_entry(
                lines,
                date=date,
                memo=memo,
                source_type="payments.refund",
                source_id=receipt.pk,
                posted_by=actor,
                association=receipt.association,
            )
        except LedgerError as exc:
            raise PaymentError(str(exc)) from exc
        refund = Refund.all_associations.create(
            association=receipt.association,
            receipt=receipt,
            amount=amount,
            date=date,
            method=method,
            reference=reference,
            reason=reason,
            entry=entry,
            created_by=actor,
        )
    return refund


# ------------------------------------------------------------------ credit


def apply_credit(
    unit: Unit, *, date: dt.date | None = None, ownership_period=None, posted_by=None
) -> list[Allocation]:
    """Apply the unit's unapplied credit to its open charges. See the module docstring."""
    association = unit.association
    made: list[Allocation] = []
    with transaction.atomic():
        unit = _lock_unit(unit)
        sources = _credit_sources(
            unit.pk, ownership_period.pk if ownership_period else None, association
        )
        for source in sources:
            left = _source_unapplied(source)
            if left <= 0:
                continue
            op = source.ownership_period
            charges = open_charges(unit, op, lock=True)
            plan = allocation_plan(association, charges, left)
            if not plan:
                continue
            total = sum((a for _c, a in plan), ZERO)
            when = max(date or association_today(association), _source_date(source))
            fund = operating_fund(association)
            lines = [
                Line(
                    account=PREPAID,
                    fund=fund,
                    debit=total,
                    unit_id=unit.pk,
                    ownership_period_id=op.pk,
                    memo=_("Credit applied"),
                ),
                *_receivable_lines(plan, op.pk, fund),
            ]
            try:
                entry = post_entry(
                    lines,
                    date=when,
                    memo=_("Credit applied · Unit %(u)s") % {"u": unit.number},
                    source_type="payments.credit_applied",
                    source_id=source.pk,
                    posted_by=_actor(posted_by),
                    association=association,
                )
            except LedgerError as exc:
                raise PaymentError(str(exc)) from exc
            made += _create_allocations(source, plan, entry)
    return made


reallocate = apply_credit


def _fund_of(charges) -> Fund:
    for charge in charges:
        line = charge.entry.lines.filter(account__code=RECEIVABLE).select_related("fund").first()
        if line:
            return line.fund
    return Fund.all_associations.get(association_id=charges[0].association_id, code=OPERATING)


def release_charge(charge: Charge, *, date: dt.date, posted_by=None):
    """Move every active allocation on ``charge`` back to its source's credit.

    One entry Dr 1100 / Cr 2100 (tagged). Write-off allocations are refused: reverse the
    write-off's purpose explicitly instead. Returns the entry or ``None``.
    """
    active = list(_active(charge.allocations.select_related("adjustment")))
    if not active:
        return None
    if any(a.adjustment and a.adjustment.kind == AdjustmentKind.WRITE_OFF for a in active):
        raise PaymentError(_("This charge has a write-off; it can't be voided."))
    total = sum((a.amount for a in active), ZERO)
    fund = _fund_of([charge])
    tag = {"unit_id": charge.unit_id, "ownership_period_id": charge.ownership_period_id}
    try:
        entry = post_entry(
            [
                Line(account=RECEIVABLE, fund=fund, debit=total, **tag),
                Line(account=PREPAID, fund=fund, credit=total, **tag),
            ],
            date=date,
            memo=_("Payments released from %(c)s") % {"c": charge.description},
            source_type="payments.release",
            source_id=charge.pk,
            posted_by=_actor(posted_by),
            association=charge.association,
        )
    except LedgerError as exc:
        raise PaymentError(str(exc)) from exc
    _release(active, entry)
    return entry


def reverse_receipt(receipt: Receipt, user=None, reason: str = "", *, date=None) -> Receipt:
    """Undo a recorded receipt (bank-match undo): alias of ``void_receipt``.

    Unwinds the cash entry, every allocation and any prepaid credit it funded that later
    charges consumed, then re-applies the unit's other credit.
    """
    return void_receipt(receipt, reason=reason or _("Bank match undone"), posted_by=user, date=date)
