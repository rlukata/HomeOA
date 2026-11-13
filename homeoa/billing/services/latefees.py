"""Late fees and interest (FIN-09). The only write path for :class:`LateFee`.

Late fee
--------
For each active :class:`LateFeePolicy` and each charge of a kind in ``applies_to``, due on
or after ``effective_start``, the job on ``as_of`` assesses one fee when
``as_of >= due_date + grace_days`` and the unpaid amount is above ``minimum_balance``:

    fee = cents(flat_amount + unpaid * percent / 100), then min(fee, rules.late_fee_cap)

With the placeholder terms ($25 flat, 10-day grace) a charge due Sep 1 gets $25.00 on
Sep 11. The cap comes from the rule pack (``late_fees.cap``), never from core; it may be
unverified, which the UI shows with a badge.

Pre-notice caps (e.g. WA RCW 64.34.364(17)(b))
-----------------------------------------------------
When the pack sets ``late_fees.pre_notice.max_amount`` and/or ``.max_percent``, a unit is in
its pre-notice window from its delinquency (the oldest unpaid due date, or its open
delinquency case's anchor) until ``delinquency.standstill_days`` after the first statutory
notice was mailed (see ``statements.delinquency.standstill_until``). Inside the window:

* at most one late fee for the unit (a waived fee does not count), and
* that fee is clamped to min(``max_amount``, ``max_percent`` % x the triggering unpaid
  assessment). A clamped fee is marked ``capped`` and its charge memo says so.

``late_fees.pre_notice.admin_fee_max`` caps an administrative fee; billing charges no
administrative fee today, so nothing here can exceed it.

Interest above ``interest.annual_rate_max`` is clamped to that rate the same way (memo
says so), inside or outside the window.

A unit whose autopay for a month was skipped because the pre-debit notice failed
(``payments.AutopayCharge`` ``skipped_notice_failed``, or rescheduled by a resent
notice whose debit did not then fail) gets no late fee on charges due that
month; interest is unchanged.

Interest
--------
On or after the first of a month, each overdue charge accrues simple interest for the days
of the previous month it was past its grace period:

    interest = cents(unpaid * interest_apr / 100 * days / 365)

posted once per (charge, month). Late fees and interest are themselves charges
(``ChargeKind.LATE_FEE`` / ``INTEREST``) only when ``applies_to`` lists them.

Ledger: Dr receivable (tagged unit + the late charge's ownership period) / Cr late fee or
interest income, in the policy's fund, ``source_type="billing.late_fee"``. The fee belongs
to the owner who owed the late charge.

Idempotency: :class:`LateFee` is unique per (policy, charge, kind, period); re-runs write
nothing.

Waiver: ``waive(fee, reason, user)`` voids the fee's charge through payments'
``adjustments.void_charge`` (reversing entry, ``Charge.void_entry``, audited Adjustment);
no code here sets ``open_balance``. A fee that has had any payment applied is refused
(unapply the payment first), so a waiver never decides where applied money goes.

Payment allocation seam
-------------------------------
``unpaid_amount(charge, as_of)`` is the only place that decides how much of a charge is
unpaid. It reads ``Charge.open_balance``, which payment allocation (the rule pack's
allocation order) maintains. If allocation later needs an as-of view (payments dated
after ``as_of`` but entered earlier), change only this function.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa import audit, rules
from homeoa.core.fields import ZERO
from homeoa.ledger.services import LedgerError, Line, post_entry

from ..latefee_models import LateFeeKind
from ..models import Charge, ChargeKind, LateFee, LateFeePolicy
from . import BillingError, cents

DAYS_IN_YEAR = Decimal(365)


def unpaid_amount(charge: Charge, as_of: dt.date) -> Decimal:
    """The seam to payment allocation: how much of ``charge`` is unpaid on ``as_of``."""
    return charge.open_balance


def fee_amount(policy: LateFeePolicy, unpaid: Decimal, cap: Decimal | None):
    """``(amount, computed, capped)`` for a late fee on ``unpaid``."""
    computed = cents(policy.flat_amount + unpaid * policy.percent / 100)
    if cap is not None and computed > cap:
        return cap, computed, True
    return computed, computed, False


def interest_amount(unpaid: Decimal, apr: Decimal, days: int) -> Decimal:
    if days <= 0 or apr <= 0 or unpaid <= 0:
        return ZERO
    return cents(unpaid * apr / 100 * Decimal(days) / DAYS_IN_YEAR)


def late_on(policy: LateFeePolicy, charge: Charge) -> dt.date:
    """The first day a fee may be charged."""
    return charge.due_date + dt.timedelta(days=policy.grace_days)


def _prev_month(as_of: dt.date) -> tuple[dt.date, dt.date]:
    end = as_of.replace(day=1) - dt.timedelta(days=1)
    return end.replace(day=1), end


@dataclass
class Assessed:
    fees: list[LateFee] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.fees)


# Unpaid violation fines never accrue late fees or interest, whatever the policy says.
NEVER_LATE = frozenset({ChargeKind.FINE})


def _candidates(policy: LateFeePolicy, kinds, start: dt.date | None = None):
    return Charge.all_associations.filter(
        association=policy.association,
        kind__in=[k for k in kinds if k not in NEVER_LATE],
        due_date__gte=start or policy.effective_start,
        void_entry__isnull=True,
        open_balance__gt=policy.minimum_balance,
    ).select_related("unit", "ownership_period")


def pre_notice_caps(association, as_of: dt.date) -> dict | None:
    """The pack's pre-notice late-fee caps in force on ``as_of``, or ``None``."""
    amount = rules.value(association, "late_fees.pre_notice.max_amount", as_of)
    percent = rules.value(association, "late_fees.pre_notice.max_percent", as_of)
    if amount is None and percent is None:
        return None
    return {
        "amount": amount,
        "percent": percent,
        "standstill": rules.value(association, "delinquency.standstill_days", as_of),
    }


def pre_notice_start(unit_id: int, association, as_of: dt.date, caps: dict) -> dt.date | None:
    """Start of the unit's pre-notice window, or ``None`` when it is over (or not begun)."""
    from homeoa.statements import delinquency as ladder
    from homeoa.statements.models import DelinquencyCase

    case = DelinquencyCase.all_associations.filter(
        association=association, unit_id=unit_id, status=DelinquencyCase.Status.OPEN
    ).first()
    if case is not None:
        until = ladder.standstill_until(case, {"standstill": caps["standstill"]})
        if until is not None and as_of > until:
            return None
        return case.anchor_due_date
    return (
        Charge.all_associations.filter(
            association=association,
            unit_id=unit_id,
            void_entry__isnull=True,
            open_balance__gt=0,
            due_date__lt=as_of,
        )
        .order_by("due_date")
        .values_list("due_date", flat=True)
        .first()
    )


def pre_notice_limit(caps: dict, unpaid: Decimal) -> Decimal | None:
    limits = []
    if caps["amount"] is not None:
        limits.append(Decimal(caps["amount"]))
    if caps["percent"] is not None:
        limits.append(cents(unpaid * Decimal(caps["percent"]) / 100))
    return min(limits) if limits else None


def _fee_in_window(policy, unit_id: int, start: dt.date) -> bool:
    return LateFee.all_associations.filter(
        association=policy.association,
        kind=LateFeeKind.LATE_FEE,
        source_charge__unit_id=unit_id,
        charge__charge_date__gte=start,
        waived_at__isnull=True,
    ).exists()


def _post(policy, source, kind, period, amount, computed, capped, as_of, posted_by=None, note=""):
    """Post one fee charge and its record; ``None`` when it already exists."""
    exists = LateFee.all_associations.filter(
        policy=policy, source_charge=source, kind=kind, period=period
    ).exists()
    if exists:
        return None
    if kind == LateFeeKind.LATE_FEE:
        income, ckind = policy.fee_income_account, ChargeKind.LATE_FEE
        description = _("Late fee · %(d)s") % {"d": source.description}
    else:
        income, ckind = policy.interest_income_account, ChargeKind.INTEREST
        description = _("Interest · %(m)s · %(d)s") % {
            "m": date_format(period, "F Y"),
            "d": source.description,
        }
    if note:
        description = f"{description} ({note})"
    try:
        with transaction.atomic():
            entry = post_entry(
                [
                    Line(
                        account=policy.receivable_account,
                        fund=policy.fund,
                        debit=amount,
                        unit_id=source.unit_id,
                        ownership_period_id=source.ownership_period_id,
                    ),
                    Line(account=income, fund=policy.fund, credit=amount, unit_id=source.unit_id),
                ],
                date=as_of,
                memo=_("%(d)s · Unit %(u)s") % {"d": description, "u": source.unit.number},
                source_type="billing.late_fee",
                source_id=f"{policy.pk}:{source.pk}:{kind}:{period.isoformat()}",
                posted_by=posted_by,
                association=policy.association,
            )
            charge = Charge.objects.create(
                association=policy.association,
                unit=source.unit,
                ownership_period=source.ownership_period,
                kind=ckind,
                description=description[:200],
                amount=amount,
                open_balance=amount,
                charge_date=as_of,
                due_date=as_of,
                period=period,
                entry=entry,
            )
            return LateFee.objects.create(
                association=policy.association,
                policy=policy,
                source_charge=source,
                kind=kind,
                period=period,
                charge=charge,
                computed=computed,
                capped=capped,
            )
    except IntegrityError:  # a concurrent run created it first
        return None
    except LedgerError as exc:
        raise BillingError(str(exc)) from exc


def notice_failed_months(association) -> set[tuple[int, dt.date]]:
    """(unit id, month) pairs whose autopay was skipped because its pre-debit notice failed
    : charges due that month get no late fee, since the owner was not at fault."""
    from django.apps import apps
    from django.db.models import Q

    charge = apps.get_model("payments", "AutopayCharge")
    # A cycle rescheduled by a resent notice is debited after the due date
    # through no fault of the owner, so it stays excused, unless that rescheduled debit
    # itself failed (an owner-side decline): then the normal rules apply again.
    return set(
        charge.all_associations.filter(association=association)
        .filter(Q(status="skipped_notice_failed") | (Q(notice_resends__gt=0) & ~Q(status="failed")))
        .values_list("unit_id", "period")
    )


def assess(
    policy: LateFeePolicy, as_of: dt.date, *, posted_by=None, not_before: dt.date | None = None
) -> Assessed:
    """Assess late fees and interest due on ``as_of`` for one policy (idempotent).

    ``not_before`` (the go-live date, from the scheduled job) makes the policy start no
    earlier than that date, exactly as if its ``effective_start`` were later."""
    result = Assessed()
    start_on = max(policy.effective_start, not_before or policy.effective_start)
    if not policy.active or as_of < start_on:
        return result
    kinds = list(policy.applies_to or [])
    with transaction.atomic():
        policy = LateFeePolicy.all_associations.select_for_update().get(pk=policy.pk)
        cap = rules.late_fee_cap(policy.association)
        pre = pre_notice_caps(policy.association, as_of)
        excused = notice_failed_months(policy.association)
        if policy.flat_amount > 0 or policy.percent > 0:
            for source in _candidates(policy, kinds, start_on).filter(
                due_date__lte=as_of - dt.timedelta(days=policy.grace_days)
            ):
                if (source.unit_id, source.due_date.replace(day=1)) in excused:
                    continue  # autopay notice failed that month: not the owner's fault
                unpaid = unpaid_amount(source, as_of)
                if unpaid <= policy.minimum_balance:
                    continue
                amount, computed, capped = fee_amount(policy, unpaid, cap)
                note = ""
                if pre is not None:
                    start = pre_notice_start(source.unit_id, policy.association, as_of, pre)
                    if start is not None:
                        if _fee_in_window(policy, source.unit_id, start):
                            continue
                        limit = pre_notice_limit(pre, unpaid)
                        if limit is not None and amount > limit:
                            amount, capped = limit, True
                            note = _("limited to $%(cap)s before the first notice") % {"cap": limit}
                if amount <= 0:
                    continue
                fee = _post(
                    policy,
                    source,
                    LateFeeKind.LATE_FEE,
                    source.due_date,
                    amount,
                    computed,
                    capped,
                    as_of,
                    posted_by,
                    note,
                )
                if fee:
                    result.fees.append(fee)
        apr, apr_note = policy.interest_apr, ""
        apr_max = rules.value(policy.association, "interest.annual_rate_max", as_of)
        if apr_max is not None and apr > Decimal(apr_max):
            apr = Decimal(apr_max)
            apr_note = _("rate limited to %(r)s%% a year") % {"r": apr.normalize()}
        if apr > 0:
            start, end = _prev_month(as_of)
            if end >= start_on:
                for source in _candidates(policy, kinds, start_on).filter(
                    due_date__lte=end - dt.timedelta(days=policy.grace_days)
                ):
                    unpaid = unpaid_amount(source, as_of)
                    if unpaid <= policy.minimum_balance:
                        continue
                    first = max(start, late_on(policy, source))
                    days = (end - first).days + 1
                    amount = interest_amount(unpaid, apr, days)
                    full = interest_amount(unpaid, policy.interest_apr, days)
                    if amount <= 0:
                        continue
                    fee = _post(
                        policy,
                        source,
                        LateFeeKind.INTEREST,
                        start,
                        amount,
                        full,
                        bool(apr_note),
                        as_of,
                        posted_by,
                        apr_note,
                    )
                    if fee:
                        result.fees.append(fee)
    return result


def assess_due(association, as_of: dt.date, *, not_before: dt.date | None = None) -> list[LateFee]:
    """Run every active policy (the periodic job ``billing.assess_late_fees``)."""
    fees: list[LateFee] = []
    for policy in LateFeePolicy.all_associations.filter(association=association, active=True):
        fees.extend(assess(policy, as_of, not_before=not_before).fees)
    return fees


def waive(fee: LateFee, *, reason: str, user=None, on: dt.date | None = None) -> LateFee:
    """Void ``fee``'s charge through the payments void path. See module doc."""
    from homeoa.payments.services import PaymentError

    from .adjustments import void_charge

    reason = (reason or "").strip()
    if not reason:
        raise BillingError(_("Give a reason for the waiver."))
    with transaction.atomic():
        fee = LateFee.all_associations.select_for_update().get(pk=fee.pk)
        if fee.waived:
            raise BillingError(_("This fee is already waived."))
        charge = Charge.all_associations.get(pk=fee.charge_id)
        if charge.open_balance != charge.amount:
            raise BillingError(
                _("A payment is applied to this fee. Unapply the payment before waiving it.")
            )
        actor = user if getattr(user, "pk", None) else None
        try:
            void_charge(
                charge,
                _("Late fee waived: %(r)s") % {"r": reason},
                date=on,
                posted_by=actor,
            )
        except (PaymentError, LedgerError) as exc:
            raise BillingError(str(exc)) from exc
        charge.refresh_from_db()
        entry = charge.void_entry
        fee.waived_at = timezone.now()
        fee.waived_by = actor
        fee.waive_reason = reason[:300]
        fee.waive_entry = entry
        fee.save(update_fields=["waived_at", "waived_by", "waive_reason", "waive_entry"])
        audit.record(
            "billing.late_fee_waived",
            obj=fee,
            association=fee.association,
            actor=actor,
            changes={"amount": str(charge.amount), "reversal_entry": entry.pk},
            message=fee.waive_reason,
        )
    return fee


def default_policy_kwargs(association) -> dict:
    """A new policy pre-filled from the rule pack (placeholders in the generic pack)."""
    from homeoa.ledger.models import Account, Fund

    accounts = Account.all_associations.filter(association=association)
    flat = rules.value(association, "late_fees.amount") or ZERO
    if rules.value(association, "late_fees.enabled") is False:
        flat = ZERO
    return {
        "name": _("Late fees"),
        "grace_days": rules.value(association, "late_fees.grace_days") or 0,
        "flat_amount": flat,
        "interest_apr": rules.value(association, "interest.annual_rate") or ZERO,
        "receivable_account": accounts.filter(code="1100").first(),
        "fee_income_account": accounts.filter(code="4100").first(),
        "interest_income_account": accounts.filter(code="4300").first(),
        "fund": Fund.all_associations.filter(association=association, code="operating").first(),
    }


def todo_items(association) -> list[dict]:
    """Ask the board to adopt an interest rate where the law otherwise charges its maximum
    (rule key ``interest.statutory_default_applies``; WA: RCW 64.34.364(13))."""
    from django.urls import reverse

    from homeoa.access import permissions as perms

    default = rules.resolve(association, "interest.statutory_default_applies")
    if not default.value:
        return []
    policies = LateFeePolicy.all_associations.filter(association=association, active=True)
    if any(p.interest_adopted_on for p in policies):
        return []
    policy = policies.order_by("pk").first()
    cap = rules.resolve(association, "interest.annual_rate_max")
    return [
        {
            "id": "interest-rate-adopt",
            "kind": "money",
            "title": _("Adopt an interest rate for late assessments"),
            "sub": _(
                "Without one, the law charges the maximum rate (%(cap)s%% a year, %(cite)s). "
                "Adopt a rate by board resolution (0%% is allowed) and record it on the late "
                "fee policy."
            )
            % {"cap": cap.value, "cite": default.citation or ""},
            "url": reverse("billing:policy_edit", args=[policy.pk])
            if policy
            else reverse("billing:policy_new"),
            "action": _("Record the rate"),
            "perm": perms.FINANCE_MANAGE,
        }
    ]
