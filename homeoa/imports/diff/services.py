"""Parallel-run diff (FIN-18): compare a stored eCondo snapshot to HomeOA's books.

Read-only against the ledger: it only reads balances and journal lines and writes
:class:`DiffRun` / :class:`DiffLine` rows. Tolerance is $0.00: any difference is listed.

* Owner balances: each unit's HomeOA balance (unit subledger) vs the snapshot's owner balance.
* GL accounts: each snapshot trial-balance account is mapped to a HomeOA account (saved
  account mappings, then the snapshot's mapping, then the same code) and summed across funds;
  amounts are shown in the account's normal direction. A receivable/prepaid account difference
  that equals the owner-balance differences is shown but not counted twice.
* Warnings: a snapshot trial balance that does not balance within a fund (after mapping) would
  need a balancing line to opening equity in that fund (the import plugs it there).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.ledger.models import Account, Fund, JournalLine
from homeoa.registry.models import Unit

from ..models import AccountMapping, ImportRun
from ..profiles import account_choice
from .models import DiffLine, DiffRun

ZERO = Decimal("0.00")
TOLERANCE = Decimal("0.00")


def _d(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return ZERO


def _date(value) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _unit_key(value) -> str:
    return str(value or "").strip()


def _money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


# ---------------------------------------------------------------- mapping


@dataclass
class Target:
    code: str
    fund: str


def account_targets(snapshot: ImportRun) -> dict[str, Target]:
    """eCondo account code -> HomeOA account code and fund code."""
    a = snapshot.association
    data = snapshot.snapshot or {}
    cfg = snapshot.config or {}
    funds = {f.code.lower(): f.code for f in Fund.all_associations.filter(association=a)}
    funds.update({f.name.lower(): f.code for f in Fund.all_associations.filter(association=a)})
    chart_fund = {
        r.get("code"): funds.get((r.get("fund") or "").strip().lower(), "")
        for r in data.get("accounts", [])
    }
    saved = {
        m.external_code: Target(m.account.code, m.fund.code)
        for m in AccountMapping.all_associations.filter(
            association=a, source=snapshot.source
        ).select_related("account", "fund")
    }
    names = {}
    for r in data.get("trial_balance", []) + data.get("accounts", []):
        if r.get("code"):
            names.setdefault(r["code"], r.get("name"))
    out = {}
    for code, name in names.items():
        if code in saved:
            out[code] = saved[code]
            continue
        choice = account_choice(cfg, code, name, snapshot.source)
        fund = choice.get("fund") or chart_fund.get(code) or "operating"
        out[code] = Target(choice.get("account") or code, fund)
    return out


# ---------------------------------------------------------------- comparison


@dataclass
class Row:
    key: str
    label: str
    homeoa: Decimal
    econdo: Decimal
    counted: bool = True

    @property
    def difference(self) -> Decimal:
        return self.econdo - self.homeoa

    @property
    def differs(self) -> bool:
        return abs(self.difference) > TOLERANCE


@dataclass
class Comparison:
    units: list[Row]
    accounts: list[Row]
    warnings: list[str]


def compare(snapshot: ImportRun) -> Comparison:
    a = snapshot.association
    as_of = snapshot.as_of
    data = snapshot.snapshot or {}

    # Owner balances.
    lines = JournalLine.all_associations.filter(
        association=a, entry__date__lte=as_of, account__in_unit_subledger=True
    )
    homeoa_units = {
        r["unit__number"]: (r["d"] or ZERO) - (r["c"] or ZERO)
        for r in lines.exclude(unit=None)
        .values("unit__number")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    }
    econdo_units: dict[str, Decimal] = defaultdict(lambda: ZERO)
    names: dict[str, str] = {}
    for r in data.get("balances", []):
        key = _unit_key(r.get("unit"))
        if key:
            econdo_units[key] += _d(r.get("balance"))
            names.setdefault(key, r.get("name") or "")
    numbers = set(Unit.all_associations.filter(association=a).values_list("number", flat=True))
    keys = numbers | set(homeoa_units) | set(econdo_units)
    units = [
        Row(k, names.get(k, ""), homeoa_units.get(k, ZERO), econdo_units.get(k, ZERO)) for k in keys
    ]
    units.sort(key=lambda r: (not r.key.isdigit(), int(r.key) if r.key.isdigit() else 0, r.key))

    # GL accounts.
    targets = account_targets(snapshot)
    accounts = {x.code: x for x in Account.all_associations.filter(association=a)}
    econdo_net: dict[str, Decimal] = defaultdict(lambda: ZERO)
    fund_net: dict[str, Decimal] = defaultdict(lambda: ZERO)
    ext_names: dict[str, str] = {}
    for r in data.get("trial_balance", []):
        code = r.get("code")
        if not code:
            continue
        t = targets.get(code) or Target(code, "operating")
        net = _d(r.get("debit")) - _d(r.get("credit"))
        econdo_net[t.code] += net
        fund_net[t.fund] += net
        ext_names.setdefault(t.code, f"{code} {r.get('name') or ''}".strip())
    homeoa_net = {
        r["account__code"]: (r["d"] or ZERO) - (r["c"] or ZERO)
        for r in JournalLine.all_associations.filter(association=a, entry__date__lte=as_of)
        .values("account__code")
        .annotate(d=Sum("debit"), c=Sum("credit"))
    }
    rows = []
    for code in set(econdo_net) | {c for c, v in homeoa_net.items() if v}:
        acct = accounts.get(code)
        sign = Decimal(1) if acct is None or acct.debit_normal else Decimal(-1)
        label = acct.name if acct else ext_names.get(code, "")
        rows.append(
            Row(code, label, sign * homeoa_net.get(code, ZERO), sign * econdo_net.get(code, ZERO))
        )
    rows.sort(key=lambda r: r.key)

    # Subledger accounts already explained by the owner-balance differences.
    unit_diff = sum((r.difference for r in units), ZERO)
    sub = [
        r for r in rows if r.differs and accounts.get(r.key) and accounts[r.key].in_unit_subledger
    ]
    sub_diff = sum((r.difference * (1 if accounts[r.key].debit_normal else -1) for r in sub), ZERO)
    if sub and sub_diff == unit_diff:
        for r in sub:
            r.counted = False

    warnings = []
    fund_names = {f.code: f.name for f in Fund.all_associations.filter(association=a)}
    for fund, net in sorted(fund_net.items()):
        if net:
            warnings.append(
                _(
                    "The imported trial balance does not balance in the %(fund)s fund "
                    "(off by %(amount)s). Its import needs a balancing line of that amount "
                    "to opening equity (3000) in that fund."
                )
                % {"fund": fund_names.get(fund, fund), "amount": _money(net)}
            )
    return Comparison(units, rows, warnings)


def period_start_for(association, as_of: dt.date) -> dt.date:
    """Start of the drill-down period: the day after the previous check, else the month start."""
    prev = (
        DiffRun.all_associations.filter(association=association, as_of__lt=as_of)
        .order_by("-as_of")
        .first()
    )
    return prev.as_of + dt.timedelta(days=1) if prev else as_of.replace(day=1)


@transaction.atomic
def run_diff(snapshot: ImportRun, user=None) -> DiffRun:
    if snapshot.status != ImportRun.Status.STORED or snapshot.mode != ImportRun.Mode.COMPARISON:
        raise ValueError("Only stored comparison snapshots can be diffed.")
    a = snapshot.association
    result = compare(snapshot)
    run = DiffRun.all_associations.create(
        association=a,
        snapshot=snapshot,
        as_of=snapshot.as_of,
        period_start=period_start_for(a, snapshot.as_of),
        warnings=result.warnings,
        units_matched=sum(1 for r in result.units if not r.differs),
        accounts_matched=sum(1 for r in result.accounts if not r.differs),
        created_by=user if getattr(user, "pk", None) else None,
    )
    DiffLine.all_associations.bulk_create(
        DiffLine(
            association=a,
            run=run,
            kind=kind,
            key=r.key,
            label=r.label[:200],
            homeoa=r.homeoa,
            econdo=r.econdo,
            difference=r.difference,
            counted=r.counted,
        )
        for kind, rows in (
            (DiffLine.Kind.UNIT, result.units),
            (DiffLine.Kind.ACCOUNT, result.accounts),
        )
        for r in rows
        if r.differs
    )
    audit.record(
        "imports.diff_run",
        run,
        changes={
            "snapshot": [None, snapshot.pk],
            "as_of": [None, run.as_of.isoformat()],
            "differences": [None, run.lines.count()],
        },
        association=a,
        actor=run.created_by,
    )
    return run


@transaction.atomic
def explain(line: DiffLine, note: str, user) -> DiffLine:
    old = line.note
    line.note = note.strip()
    line.explained_by = user if getattr(user, "pk", None) else None
    line.explained_at = timezone.now() if line.note else None
    line.save(update_fields=["note", "explained_by", "explained_at"])
    audit.record(
        "imports.diff_explain",
        line,
        changes={"note": [old, line.note]},
        association=line.association,
        actor=line.explained_by,
    )
    return line


# ---------------------------------------------------------------- drill-down


def drilldown(line: DiffLine) -> dict:
    """HomeOA journal lines vs the imported ledger lines for the line's unit or account."""
    run = line.run
    a = run.association
    start = run.period_start
    snap = run.snapshot
    data = snap.snapshot or {}
    qs = JournalLine.all_associations.filter(
        association=a, entry__date__gte=start, entry__date__lte=run.as_of
    ).select_related("entry", "account", "fund")
    econdo = []
    if line.kind == DiffLine.Kind.UNIT:
        qs = qs.filter(unit__number=line.key, account__in_unit_subledger=True)
        for r in data.get("history", []):
            d = _date(r.get("date"))
            if _unit_key(r.get("unit")) == line.key and d and d >= start:
                econdo.append(
                    {
                        "date": d,
                        "type": r.get("type", ""),
                        "description": r.get("description", ""),
                        "amount": _d(r.get("charge")) - _d(r.get("payment")),
                    }
                )
    else:
        qs = qs.filter(account__code=line.key)
        types = (snap.config or {}).get("history_types", {})
        acct = Account.all_associations.filter(association=a, code=line.key).first()
        sign = Decimal(-1) if acct is not None and not acct.debit_normal else Decimal(1)
        for r in data.get("history", []):
            d = _date(r.get("date"))
            if types.get(r.get("type", "")) == line.key and d and d >= start:
                # The counter account takes the opposite side of the unit's charge/payment.
                amount = -(_d(r.get("charge")) - _d(r.get("payment")))
                econdo.append(
                    {
                        "date": d,
                        "type": r.get("type", ""),
                        "description": f"{_('Unit')} {r.get('unit')}: {r.get('description', '')}",
                        "amount": sign * amount,
                    }
                )
    homeoa = []
    for jl in qs.order_by("entry__date", "pk"):
        amount = jl.debit - jl.credit
        if line.kind == DiffLine.Kind.ACCOUNT and not jl.account.debit_normal:
            amount = -amount
        homeoa.append(
            {"date": jl.entry.date, "description": jl.memo or jl.entry.memo, "amount": amount}
        )
    econdo.sort(key=lambda r: r["date"])
    return {"start": start, "end": run.as_of, "homeoa": homeoa, "econdo": econdo}
