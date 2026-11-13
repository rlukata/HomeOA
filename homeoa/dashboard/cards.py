"""Money card / Money overview figures, computed from the books (RPT-02).

* Cash: the ledger balance of each bank account's cash GL account in its fund, summed
  into operating and reserve (fallback with no bank accounts: every cash account the
  template defines, 1000 and 1010).
* Dues: the latest posted assessment month on or before today. Billed = its assessment
  charges (voided charges excluded); collected = billed minus what is still open.
* Pending match: unmatched bank deposits waiting to be matched to a unit, capped at what is
  still open for the month.
* Past due: open balances of charges whose due date has passed, per unit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Max, Min, Sum

ZERO = Decimal("0.00")
HUNDRED = Decimal(100)


@dataclass
class PastDueUnit:
    unit: object
    amount: Decimal
    since: dt.date


@dataclass
class MoneySummary:
    operating: Decimal = ZERO
    reserve: Decimal = ZERO
    last_sync: dt.datetime | None = None
    dues_period: dt.date | None = None
    billed: Decimal = ZERO
    collected: Decimal = ZERO
    pending_match: Decimal = ZERO
    past_due: list[PastDueUnit] = field(default_factory=list)

    @property
    def cash(self) -> Decimal:
        return self.operating + self.reserve

    @property
    def outstanding(self) -> Decimal:
        return self.billed - self.collected

    def _pct(self, part: Decimal) -> int:
        if not self.billed:
            return 0
        return int((part * HUNDRED / self.billed).to_integral_value())

    @property
    def collected_pct(self) -> int:
        return self._pct(self.collected)

    @property
    def pending_pct(self) -> int:
        return min(self._pct(self.pending_match), 100 - self.collected_pct)

    @property
    def past_due_total(self) -> Decimal:
        return sum((p.amount for p in self.past_due), ZERO)


def cash_by_fund(association, *, as_of: dt.date | None = None) -> list[tuple[object, Decimal]]:
    """``(fund, cash)`` per fund, in fund order (the board dashboard's cash card), today or
    at the end of ``as_of``."""
    from homeoa.banking.models import BankAccount
    from homeoa.ledger.models import Account, Fund
    from homeoa.ledger.services import account_balance

    pairs = {
        (ba.gl_account_id, ba.fund_id): (ba.gl_account, ba.fund)
        for ba in BankAccount.all_associations.filter(
            association=association, active=True
        ).select_related("gl_account", "fund")
    }
    if not pairs:
        funds = list(Fund.all_associations.filter(association=association))
        for acct in Account.all_associations.filter(
            association=association, code__in=("1000", "1010")
        ):
            for fund in funds:
                pairs[(acct.pk, fund.pk)] = (acct, fund)
    totals: dict = {}
    for acct, fund in pairs.values():
        totals[fund] = totals.get(fund, ZERO) + account_balance(acct, fund=fund, as_of=as_of)
    return sorted(totals.items(), key=lambda kv: (kv[0].sort_order, kv[0].code))


def _cash(association) -> tuple[Decimal, Decimal]:
    operating = reserve = ZERO
    for fund, bal in cash_by_fund(association):
        if fund.is_reserve:
            reserve += bal
        else:
            operating += bal
    return operating, reserve


def _last_sync(association):
    from homeoa.banking.models import BankAccount

    return BankAccount.all_associations.filter(association=association).aggregate(
        m=Max("last_synced_at")
    )["m"]


def _dues(association, today):
    from homeoa.billing.models import AdjustmentKind, BillingRun, Charge, ChargeKind

    period = (
        BillingRun.all_associations.filter(association=association, period__lte=today)
        .aggregate(m=Max("period"))
        .get("m")
    )
    if period is None:
        return None, ZERO, ZERO
    charges = Charge.all_associations.filter(
        association=association, kind=ChargeKind.ASSESSMENT, period=period
    ).exclude(adjustments__kind=AdjustmentKind.CHARGE_VOID)
    agg = charges.aggregate(a=Sum("amount"), o=Sum("open_balance"))
    billed = agg["a"] or ZERO
    return period, billed, billed - (agg["o"] or ZERO)


def pending_deposits(association) -> list:
    """Unmatched bank deposits that look like owner payments.

    With the matching engine only deposits whose best suggestion is a unit or a
    recorded payment count; without it, every unmatched deposit does.
    """
    from homeoa.banking.models import BankTransaction, TxnStatus

    txns = list(
        BankTransaction.all_associations.filter(
            association=association, status=TxnStatus.UNMATCHED, amount__gt=0
        )
    )
    try:
        from homeoa.banking import matching
    except ImportError:
        return txns
    out = []
    for txn in txns:
        best = next(iter(matching.suggestions(txn)), None)
        if best is not None and getattr(best, "kind", "") in ("unit", "link"):
            out.append(txn)
    return out


def _past_due(association, today) -> list[PastDueUnit]:
    from homeoa.billing.models import Charge
    from homeoa.registry.models import Unit

    rows = list(
        Charge.all_associations.filter(
            association=association, open_balance__gt=0, due_date__lt=today
        )
        .values("unit_id")
        .annotate(amount=Sum("open_balance"), since=Min("charge_date"))
    )
    units = Unit.all_associations.in_bulk([r["unit_id"] for r in rows])
    out = [PastDueUnit(units[r["unit_id"]], r["amount"], r["since"]) for r in rows]
    return sorted(out, key=lambda p: (-p.amount, p.unit.sort_key))


def money_summary(association, today: dt.date) -> MoneySummary:
    summary = MoneySummary()
    if association is None:
        return summary
    summary.operating, summary.reserve = _cash(association)
    summary.last_sync = _last_sync(association)
    summary.dues_period, summary.billed, summary.collected = _dues(association, today)
    pending = sum((t.amount for t in pending_deposits(association)), ZERO)
    summary.pending_match = min(pending, max(summary.outstanding, ZERO))
    summary.past_due = _past_due(association, today)
    return summary


# ------------------------------------------------------------ board dashboard (RPT-02)

AP_DUE_DAYS = 14


@dataclass
class BoardCards:
    funds: list = field(default_factory=list)  # (fund, cash)
    budget: object = None  # budget.services.BudgetVsActual or None
    ap_due: list = field(default_factory=list)  # Bills due within AP_DUE_DAYS (or overdue)
    ap_due_until: dt.date | None = None
    compliance_open: int = 0
    compliance_missed: int = 0

    @property
    def ap_due_total(self) -> Decimal:
        return sum((b.total for b in self.ap_due), ZERO)


def _budget(association, today):
    from homeoa.budget import services as budget

    month = budget.fiscal_start_month(association)
    year = today.year if today.month >= month else today.year - 1
    adopted = budget.adopted_for(association, year)
    return budget.budget_vs_actual(adopted, today) if adopted else None


def _ap_due(association, until):
    from homeoa.ap.models import Bill

    return list(
        Bill.all_associations.filter(
            association=association,
            status__in=(Bill.Status.PENDING, Bill.Status.APPROVED),
            due_date__lte=until,
        )
        .select_related("vendor")
        .prefetch_related("lines")
        .order_by("due_date", "pk")
    )


def _compliance(association) -> tuple[int, int]:
    from homeoa.compliance.models import ObligationPeriod, PeriodStatus

    open_periods = list(
        ObligationPeriod.all_associations.filter(association=association, status=PeriodStatus.OPEN)
    )
    return len(open_periods), sum(1 for p in open_periods if p.missed)


def board_cards(association, today: dt.date, *, compliance: bool = True) -> BoardCards:
    """The board dashboard's cards; read-only, from the existing services."""
    out = BoardCards()
    if association is None:
        return out
    out.funds = cash_by_fund(association)
    out.budget = _budget(association, today)
    out.ap_due_until = today + dt.timedelta(days=AP_DUE_DAYS)
    out.ap_due = _ap_due(association, out.ap_due_until)
    if compliance:
        out.compliance_open, out.compliance_missed = _compliance(association)
    return out
