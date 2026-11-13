"""Credit memos, write-offs, manual charges and charge voids (FIN-11).

Every function needs a non-empty ``reason``, writes one audited
:class:`~homeoa.billing.models.Adjustment` and posts through ``ledger.services.post_entry``
(via the payments allocation engine for credits). Callers check ``finance.manage``.

* ``credit_memo(unit, amount, reason, ...)``: Dr ``account`` (default 4000 assessment
  income) / Cr 1100 for the charges it pays (``charge`` first, then the configured order) /
  Cr 2100 prepaid for any excess, which later charges consume like a prepayment.
* ``write_off(unit, amount, reason, ...)``: Dr bad-debt expense (default 5800, created from
  the template below if the chart lacks it) / Cr 1100. Never more than the open charges.
* ``manual_charge(unit, amount, reason, description, ...)``: a new charge (e.g. owner
  damage): Dr 1100 / Cr ``account`` (default 4200 other income). Existing credit applies.
* ``void_charge(charge, reason, ...)``: payments on the charge move back to credit
  (Dr 1100 / Cr 2100), the charge entry is reversed, and the credit re-applies to other
  open charges (re-allocation when charges change).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop as N_

from homeoa.audit.services import record
from homeoa.core.context import association_today
from homeoa.ledger.models import Account, AccountType, Fund
from homeoa.ledger.services import LedgerError, Line, post_entry, reverse_entry
from homeoa.registry.services import ownership_period_on

from ..models import Adjustment, AdjustmentKind, Charge, ChargeKind

BAD_DEBT = ("5800", N_("Bad debt expense"))
CREDIT_MEMO_ACCOUNT = "4000"
MANUAL_CHARGE_ACCOUNT = "4200"
OPERATING = "operating"


def _payments():
    # payments depends on billing models; import lazily to keep the app graph acyclic.
    from homeoa.payments import services

    return services


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise _payments().PaymentError(_("Every adjustment needs a reason."))
    return reason


def _account(association, value, default) -> Account:
    value = value if value is not None else default
    if isinstance(value, Account):
        if value.association_id != association.pk:
            raise _payments().PaymentError(_("That account belongs to another association."))
        return value
    try:
        return Account.all_associations.get(association=association, code=value)
    except Account.DoesNotExist:
        raise _payments().PaymentError(_("Unknown account %(c)s.") % {"c": value}) from None


def bad_debt_account(association) -> Account:
    code, name = BAD_DEBT
    account, _created = Account.all_associations.get_or_create(
        association=association, code=code, defaults={"name": name, "type": AccountType.EXPENSE}
    )
    return account


def _operating(association) -> Fund:
    return Fund.all_associations.get(association=association, code=OPERATING)


def _period(unit, date, ownership_period, charge=None):
    if charge is not None:
        return charge.ownership_period
    op = ownership_period or ownership_period_on(unit, date)
    if op is None:
        raise _payments().PaymentError(
            _("Unit %(u)s has no owner of record on that date.") % {"u": unit}
        )
    return op


def _credit(kind, unit, amount, reason, *, account, date, charge, ownership_period, posted_by):
    pay = _payments()
    amount = pay.check_amount(amount)
    reason = _reason(reason)
    association = unit.association
    date = date or association_today(association)
    if charge is not None and charge.unit_id != unit.pk:
        raise pay.PaymentError(_("That charge belongs to another unit."))
    with transaction.atomic():
        unit = pay._lock_unit(unit)
        adj = Adjustment.all_associations.create(
            association=association,
            kind=kind,
            unit=unit,
            ownership_period=_period(unit, date, ownership_period, charge),
            amount=amount,
            date=date,
            reason=reason,
            account=account,
            charge=charge,
            created_by=pay._actor(posted_by),
        )
        label = f"{adj.get_kind_display()} · {reason}"
        entry, _allocs = pay.post_credit_source(
            adj,
            debit=Line(account=account, fund=_operating(association), debit=amount),
            amount=amount,
            date=date,
            memo=label[:200],
            source_type=f"billing.{kind}",
            posted_by=posted_by,
            first_charge=charge,
            allow_excess=kind == AdjustmentKind.CREDIT_MEMO,
        )
        adj.entry = entry
        adj.save(update_fields=["entry"])
    return adj


def credit_memo(
    unit, amount: Decimal, reason: str, *, account=None, date=None, charge=None,
    ownership_period=None, posted_by=None,
) -> Adjustment:  # fmt: skip
    account = _account(unit.association, account, CREDIT_MEMO_ACCOUNT)
    return _credit(
        AdjustmentKind.CREDIT_MEMO, unit, amount, reason, account=account, date=date,
        charge=charge, ownership_period=ownership_period, posted_by=posted_by,
    )  # fmt: skip


def write_off(
    unit, amount: Decimal, reason: str, *, account=None, date=None, charge=None,
    ownership_period=None, posted_by=None,
) -> Adjustment:  # fmt: skip
    if account is None:
        account = bad_debt_account(unit.association)
    account = _account(unit.association, account, None)
    return _credit(
        AdjustmentKind.WRITE_OFF, unit, amount, reason, account=account, date=date,
        charge=charge, ownership_period=ownership_period, posted_by=posted_by,
    )  # fmt: skip


def manual_charge(
    unit,
    amount: Decimal,
    reason: str,
    description: str,
    *,
    kind: str = ChargeKind.OTHER,
    account=None,
    date: dt.date | None = None,
    due_date: dt.date | None = None,
    ownership_period=None,
    posted_by=None,
) -> Charge:
    """Bill a one-off charge. The new charge consumes existing credit automatically."""
    pay = _payments()
    amount = pay.check_amount(amount)
    reason = _reason(reason)
    description = (description or "").strip() or reason[:200]
    association = unit.association
    account = _account(association, account, MANUAL_CHARGE_ACCOUNT)
    date = date or association_today(association)
    actor = pay._actor(posted_by)
    with transaction.atomic():
        unit = pay._lock_unit(unit)
        op = _period(unit, date, ownership_period)
        fund = _operating(association)
        try:
            entry = post_entry(
                [
                    Line(
                        account=pay.RECEIVABLE,
                        fund=fund,
                        debit=amount,
                        unit_id=unit.pk,
                        ownership_period_id=op.pk,
                    ),
                    Line(account=account, fund=fund, credit=amount, unit_id=unit.pk),
                ],
                date=date,
                memo=f"{description} · {_('Unit %(u)s') % {'u': unit.number}}"[:200],
                source_type="billing.manual_charge",
                source_id=unit.pk,
                posted_by=actor,
                association=association,
            )
        except LedgerError as exc:
            raise pay.PaymentError(str(exc)) from exc
        adj = Adjustment.all_associations.create(
            association=association,
            kind=AdjustmentKind.MANUAL_CHARGE,
            unit=unit,
            ownership_period=op,
            amount=amount,
            date=date,
            reason=reason,
            account=account,
            entry=entry,
            created_by=actor,
        )
        # Creating the charge applies existing credit (payments' post_save hook).
        charge = Charge.all_associations.create(
            association=association,
            unit=unit,
            ownership_period=op,
            kind=kind,
            description=description[:200],
            amount=amount,
            open_balance=amount,
            charge_date=date,
            due_date=due_date or date,
            entry=entry,
        )
        adj.charge = charge
        adj.save(update_fields=["charge"])
    return Charge.all_associations.get(pk=charge.pk)


def void_charge(charge: Charge, reason: str, *, date=None, posted_by=None) -> Adjustment:
    """Cancel a charge; money already applied to it becomes credit and re-applies."""
    pay = _payments()
    reason = _reason(reason)
    association = charge.association
    date = date or association_today(association)
    actor = pay._actor(posted_by)
    with transaction.atomic():
        pay._lock_unit(charge.unit)
        charge = Charge.all_associations.select_for_update().get(pk=charge.pk)
        if charge.void_entry_id:
            raise pay.PaymentError(_("This charge is already void."))
        pay.release_charge(charge, date=date, posted_by=posted_by)
        try:
            rev = reverse_entry(
                charge.entry,
                date=date,
                memo=_("Void of charge: %(c)s") % {"c": charge.description},
                posted_by=actor,
            )
        except LedgerError as exc:
            raise pay.PaymentError(str(exc)) from exc
        income = rev.lines.filter(debit__gt=0).exclude(account__code=pay.RECEIVABLE).first()
        Charge.all_associations.filter(pk=charge.pk).update(void_entry=rev, open_balance=0)
        adj = Adjustment.all_associations.create(
            association=association,
            kind=AdjustmentKind.CHARGE_VOID,
            unit=charge.unit,
            ownership_period=charge.ownership_period,
            amount=charge.amount,
            date=date,
            reason=reason,
            account=income.account if income else _account(association, None, "4000"),
            charge=charge,
            entry=rev,
            created_by=actor,
        )
        record("billing.charge_void", charge, actor=actor, message=reason)
        pay.apply_credit(charge.unit, date=date, posted_by=posted_by)
    return adj
