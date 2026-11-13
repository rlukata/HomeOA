"""Assessment billing services (FIN-03). The only write path for schedules' charges.

Amounts (fixed per-unit dues)
-----------------------------------
Each unit pays the same amount every month, rounded ROUND_HALF_UP to the cent on its own;
the run total is the sum of the unit amounts and may differ from 1/12 of the budget by a few
cents (2026 pilot: $3,432.36 a month against $41,188.49 a year). There is no remainder month
and no exact split for owner dues. ``unit_dues(schedule, unit)`` per method:

* ``allocated_interest``: ``annual_amount x interest% / 12`` (or ``period_amount x
  interest%``). Posting is refused while the interests do not total 100%.
* ``flat``: ``annual_amount / 12 / units`` (or ``period_amount / units``).
* ``unit_type``: ``type_amounts[unit_types[unit.number]]``.

``allocate(total, weights)`` (exact split: rounded shares, leftover cents to the largest
exact shares first) stays for special assessments, which must sum to the approved total.

``monthly_share(annual, period, fiscal_start_month)`` is for internal fund transfers only
(reserve, insurance): months 1-11 of the fiscal year take ``annual / 12`` rounded half-up;
the last fiscal month takes the remainder, so the twelve months sum exactly to ``annual``.

Posting
-------
``preview(schedule, period) -> Preview`` computes rows (unit, ownership period, owners,
amount), the month's reserve contribution and ``problems`` without writing anything.

``generate_assessments(schedule, period, *, posted_by=None, source="manual") -> RunResult``
posts, per unit, one :class:`~homeoa.billing.models.Charge` and one ledger entry, entirely
in the operating fund: Dr 1100 receivable (tagged ``unit_id`` and the ownership period
in effect on the charge date) / Cr 4000 income (``source_type="billing.charge"``). The charge
date is the schedule's ``post_day`` in that month; the due date its ``due_day``.

Reserve contribution: once per run, when the month's share of
``reserve_annual_amount`` is above zero, one interfund entry
(``source_type="billing.reserve"``) posts, balanced within each fund:

* operating: Dr 5900 Reserve contributions / Cr 2900 Due to other fund
* reserve:   Dr 1900 Due from other fund   / Cr 4900 Reserve contributions received

Insurance transfer: the same pattern for ``insurance_annual_amount`` into the
insurance fund (``source_type="billing.insurance"``):

* operating: Dr 5910 Transfer to insurance  / Cr 2920 Due to other fund (insurance)
* insurance: Dr 1920 Due from other fund (insurance) / Cr 4910 Insurance transfer income

Moving the cash between bank accounts later clears the due-from/due-to pair; that is
matching work in banking, not posted here.

Idempotency: a :class:`~homeoa.billing.models.BillingRun` is unique per (schedule, period)
and owns the charges and the reserve and insurance entries. Generation locks the schedule
row, and if the run exists returns it with ``created=False`` and writes nothing. Problems raise
:class:`BillingError` and nothing is posted.

``post_due_assessments(association, as_of)`` posts every active schedule whose month of
``as_of`` is covered and whose ``post_day`` has been reached (the periodic job
``billing.post_assessments``).

Transfers: ``block_transfer_over_posted_charges`` is registered as a registry transfer
validator; it refuses a transfer dated on or before a charge already posted to the old
ownership period.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa.core.fields import ZERO
from homeoa.ledger.services import LedgerError, Line, post_entry
from homeoa.registry import services as registry
from homeoa.registry.models import OwnershipPeriod, Unit

from ..models import AssessmentSchedule, BillingRun, Charge, ChargeKind, Method

CENT = Decimal("0.01")


class BillingError(ValueError):
    """Assessments could not be computed or posted; nothing was written."""


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(total: Decimal, weights: Sequence[tuple[Hashable, Decimal]]) -> dict:
    """Split ``total`` by weight into cents that sum exactly to ``total``. See module doc."""
    total = Decimal(total)
    if total != total.quantize(CENT):
        raise BillingError(_("The total must be in whole cents."))
    if not weights:
        return {}
    weight_sum = sum((Decimal(w) for _k, w in weights), ZERO)
    if weight_sum <= 0 or any(Decimal(w) < 0 for _k, w in weights):
        raise BillingError(_("Weights must be zero or more and not all zero."))
    exact = [(k, total * Decimal(w) / weight_sum) for k, w in weights]
    shares = {k: cents(x) for k, x in exact}
    leftover = int((total - sum(shares.values(), ZERO)) / CENT)
    order = [k for _i, (k, _x) in sorted(enumerate(exact), key=lambda t: (-t[1][1], t[0]))]
    step = CENT if leftover > 0 else -CENT
    for i in range(abs(leftover)):
        shares[order[i % len(order)]] += step
    return shares


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def parse_period(value: str) -> dt.date:
    """``"2026-10"`` (or any ISO date) -> ``date(2026, 10, 1)``."""
    try:
        if len(value) == 7:
            return dt.date.fromisoformat(value + "-01")
        return month_start(dt.date.fromisoformat(value))
    except TypeError, ValueError:
        raise BillingError(_("Enter a month as YYYY-MM.")) from None


def monthly_share(annual: Decimal, period: dt.date, fiscal_start_month: int = 1) -> Decimal:
    """One month of ``annual``; the last fiscal month takes the remainder. See module doc."""
    regular = cents(annual / 12)
    last_month = (fiscal_start_month - 2) % 12 + 1
    if period.month == last_month:
        return annual - regular * 11
    return regular


def _fiscal_start(schedule) -> int:
    return schedule.association.fiscal_year_start_month or 1


def _monthly_base(schedule: AssessmentSchedule) -> Decimal:
    """The unrounded association total for one month (owner dues)."""
    if schedule.period_amount is not None:
        return schedule.period_amount
    if schedule.annual_amount is not None:
        return schedule.annual_amount / 12
    return ZERO


def monthly_reserve(schedule: AssessmentSchedule, period: dt.date) -> Decimal:
    if not schedule.reserve_annual_amount:
        return ZERO
    return monthly_share(schedule.reserve_annual_amount, period, _fiscal_start(schedule))


def monthly_insurance(schedule: AssessmentSchedule, period: dt.date) -> Decimal:
    if not schedule.insurance_annual_amount:
        return ZERO
    return monthly_share(schedule.insurance_annual_amount, period, _fiscal_start(schedule))


@dataclass
class PreviewRow:
    unit: Unit
    ownership_period: OwnershipPeriod | None
    owners: list
    amount: Decimal


@dataclass
class Preview:
    schedule: AssessmentSchedule
    period: dt.date
    charge_date: dt.date
    due_date: dt.date
    reserve_amount: Decimal = ZERO
    insurance_amount: Decimal = ZERO
    rows: list[PreviewRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((r.amount for r in self.rows), ZERO)


def _natural(number: str):
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


def _units(association) -> list[Unit]:
    units = list(Unit.all_associations.filter(association=association))
    return sorted(units, key=lambda u: (u.building, _natural(u.number)))


def _amounts(schedule, period, units: list[Unit], problems: list[str]) -> dict:
    if schedule.method == Method.UNIT_TYPE:
        out = {}
        for unit in units:
            kind = (schedule.unit_types or {}).get(unit.number)
            raw = (schedule.type_amounts or {}).get(kind) if kind else None
            if raw is None:
                problems.append(_("Unit %(u)s has no unit type amount.") % {"u": unit.number})
                continue
            out[unit.pk] = Decimal(str(raw))
        return out
    base = _monthly_base(schedule)
    if schedule.method == Method.FLAT:
        return {u.pk: cents(base / len(units)) for u in units}
    if not registry.interest_total_ok(schedule.association):
        problems.append(
            _("Allocated interests total %(t)s%%, not 100%%. Fix them on the Units page first.")
            % {"t": registry.interest_total(schedule.association)}
        )
        return {}
    return {u.pk: cents(base * u.allocated_interest / 100) for u in units}


def unit_dues(schedule: AssessmentSchedule, units: list[Unit] | None = None) -> dict:
    """Unit pk -> the fixed monthly dues (problems ignored). See module doc."""
    units = _units(schedule.association) if units is None else units
    return _amounts(schedule, None, units, [])


def preview(schedule: AssessmentSchedule, period: dt.date) -> Preview:
    """What posting ``period`` would charge, without writing. See module doc."""
    period = month_start(period)
    result = Preview(
        schedule=schedule,
        period=period,
        charge_date=period.replace(day=schedule.post_day),
        due_date=period.replace(day=schedule.due_day),
        reserve_amount=monthly_reserve(schedule, period),
        insurance_amount=monthly_insurance(schedule, period),
    )
    if not schedule.covers(period):
        result.problems.append(_("The schedule is not in effect for this month."))
    units = _units(schedule.association)
    if not units:
        result.problems.append(_("There are no units to bill."))
        return result
    amounts = _amounts(schedule, period, units, result.problems)
    for unit in units:
        amount = amounts.get(unit.pk)
        if not amount:
            continue
        op = registry.ownership_period_on(unit, result.charge_date)
        if op is None:
            result.problems.append(
                _("Unit %(u)s has no owner of record on %(d)s.")
                % {"u": unit.number, "d": result.charge_date.isoformat()}
            )
        owners = registry.current_owners(unit, result.charge_date) if op else []
        result.rows.append(PreviewRow(unit=unit, ownership_period=op, owners=owners, amount=amount))
    return result


@dataclass
class RunResult:
    run: BillingRun
    created: bool

    @property
    def charges_created(self) -> int:
        return self.run.charge_count if self.created else 0


def _description(period: dt.date) -> str:
    return _("Monthly dues · %(month)s") % {"month": date_format(period, "F Y")}


def generate_assessments(
    schedule: AssessmentSchedule,
    period: dt.date,
    *,
    posted_by=None,
    source: str = "manual",
    not_before: dt.date | None = None,
) -> RunResult:
    """Post ``period`` for ``schedule`` once. See module doc for idempotency.

    ``not_before`` (the go-live date, from the scheduled job) moves a charge date that would
    fall before it onto it; due dates and amounts are unchanged."""
    period = month_start(period)
    with transaction.atomic():
        schedule = AssessmentSchedule.all_associations.select_for_update().get(pk=schedule.pk)
        existing = BillingRun.all_associations.filter(schedule=schedule, period=period).first()
        if existing is not None:
            return RunResult(existing, created=False)
        if not schedule.active:
            raise BillingError(_("This schedule is not active."))
        plan = preview(schedule, period)
        if plan.problems:
            raise BillingError(" ".join(plan.problems))
        if not plan.rows:
            raise BillingError(_("Nothing to charge for this month."))
        if not_before is not None and plan.charge_date < not_before:
            plan = dataclasses.replace(plan, charge_date=not_before)
        actor = posted_by if getattr(posted_by, "pk", None) else None
        run = BillingRun.objects.create(
            association=schedule.association,
            schedule=schedule,
            period=period,
            charge_date=plan.charge_date,
            posted_by=actor,
            source=source,
            total=plan.total,
            charge_count=len(plan.rows),
            reserve_amount=plan.reserve_amount,
            insurance_amount=plan.insurance_amount,
        )
        description = _description(period)
        try:
            for row in plan.rows:
                entry = post_entry(
                    _charge_lines(schedule, row),
                    date=plan.charge_date,
                    memo=_("%(d)s · Unit %(u)s") % {"d": description, "u": row.unit.number},
                    source_type="billing.charge",
                    source_id=f"{run.pk}:{row.unit.pk}",
                    posted_by=actor,
                    association=schedule.association,
                )
                Charge.objects.create(
                    association=schedule.association,
                    unit=row.unit,
                    ownership_period=row.ownership_period,
                    kind=ChargeKind.ASSESSMENT,
                    description=description,
                    amount=row.amount,
                    open_balance=row.amount,
                    charge_date=plan.charge_date,
                    due_date=plan.due_date,
                    period=period,
                    run=run,
                    entry=entry,
                )
            if plan.reserve_amount > 0:
                run.reserve_entry = post_entry(
                    _reserve_lines(schedule, plan.reserve_amount),
                    date=plan.charge_date,
                    memo=_("Reserve contribution · %(month)s")
                    % {"month": date_format(period, "F Y")},
                    source_type="billing.reserve",
                    source_id=run.pk,
                    posted_by=actor,
                    association=schedule.association,
                )
                run.save(update_fields=["reserve_entry"])
            if plan.insurance_amount > 0:
                run.insurance_entry = post_entry(
                    _insurance_lines(schedule, plan.insurance_amount),
                    date=plan.charge_date,
                    memo=_("Transfer to insurance · %(month)s")
                    % {"month": date_format(period, "F Y")},
                    source_type="billing.insurance",
                    source_id=run.pk,
                    posted_by=actor,
                    association=schedule.association,
                )
                run.save(update_fields=["insurance_entry"])
        except LedgerError as exc:
            raise BillingError(str(exc)) from exc
    return RunResult(run, created=True)


def _charge_lines(schedule: AssessmentSchedule, row: PreviewRow) -> list[Line]:
    fund = schedule.operating_fund
    return [
        Line(
            account=schedule.receivable_account,
            fund=fund,
            debit=row.amount,
            unit_id=row.unit.pk,
            ownership_period_id=row.ownership_period.pk,
        ),
        Line(account=schedule.income_account, fund=fund, credit=row.amount, unit_id=row.unit.pk),
    ]


def _reserve_lines(schedule: AssessmentSchedule, amount: Decimal) -> list[Line]:
    op, res = schedule.operating_fund, schedule.reserve_fund
    return [
        Line(account=schedule.reserve_expense_account, fund=op, debit=amount),
        Line(account=schedule.due_to_reserve_account, fund=op, credit=amount),
        Line(account=schedule.due_from_operating_account, fund=res, debit=amount),
        Line(account=schedule.reserve_income_account, fund=res, credit=amount),
    ]


def _insurance_lines(schedule: AssessmentSchedule, amount: Decimal) -> list[Line]:
    op, ins = schedule.operating_fund, schedule.insurance_fund
    if ins is None:
        raise BillingError(_("Choose the insurance fund and its transfer accounts."))
    return [
        Line(account=schedule.insurance_expense_account, fund=op, debit=amount),
        Line(account=schedule.due_to_insurance_account, fund=op, credit=amount),
        Line(account=schedule.insurance_due_from_operating_account, fund=ins, debit=amount),
        Line(account=schedule.insurance_income_account, fund=ins, credit=amount),
    ]


def schedules_due(association, as_of: dt.date) -> list[AssessmentSchedule]:
    period = month_start(as_of)
    return [
        s
        for s in AssessmentSchedule.all_associations.filter(association=association, active=True)
        if s.covers(period) and as_of.day >= s.post_day
    ]


def post_due_assessments(
    association, as_of: dt.date, *, not_before: dt.date | None = None
) -> list[RunResult]:
    """Post this month for every due schedule (idempotent). Used by the periodic job.

    With ``not_before`` (the go-live date), a month whose due date falls before it is not
    posted, and a charge date before it is moved onto it."""
    period = month_start(as_of)
    return [
        generate_assessments(s, as_of, source="scheduled", not_before=not_before)
        for s in schedules_due(association, as_of)
        if not_before is None or period.replace(day=s.due_day) >= not_before
    ]


def default_schedule_kwargs(association) -> dict:
    """Ledger defaults for a new schedule from the chart of accounts."""
    from homeoa.ledger.models import Account, Fund

    accounts = Account.all_associations.filter(association=association)
    funds = Fund.all_associations.filter(association=association)

    def acct(code):
        return accounts.filter(code=code).first()

    return {
        "receivable_account": acct("1100"),
        "income_account": acct("4000"),
        "reserve_expense_account": acct("5900"),
        "due_to_reserve_account": acct("2900"),
        "due_from_operating_account": acct("1900"),
        "reserve_income_account": acct("4900"),
        "insurance_expense_account": acct("5910"),
        "due_to_insurance_account": acct("2920"),
        "insurance_due_from_operating_account": acct("1920"),
        "insurance_income_account": acct("4910"),
        "insurance_fund": funds.filter(code="insurance").first(),
        "operating_fund": funds.filter(code="operating").first(),
        "reserve_fund": funds.filter(is_reserve=True).order_by("sort_order").first(),
    }


def block_transfer_over_posted_charges(unit, owners, effective_date) -> None:
    """Registry transfer validator: the old owner keeps charges dated before the transfer."""
    current = OwnershipPeriod.all_associations.filter(unit=unit, end__isnull=True).first()
    if current is None:
        return
    last = (
        Charge.all_associations.filter(ownership_period=current, charge_date__gte=effective_date)
        .order_by("-charge_date")
        .first()
    )
    if last is not None:
        raise ValidationError(
            _(
                "Charges dated through %(date)s are already posted to the current owner. "
                "Choose an effective date after %(date)s."
            )
            % {"date": last.charge_date.isoformat()}
        )
