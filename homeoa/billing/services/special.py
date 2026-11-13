"""Special assessments (FIN-10). The only write path for special assessment charges.

``create_special(association, *, name, total, method, installments, operating_fund,
for_reserves, ...)`` stores the
assessment and its installments. ``installments`` is a list of ``(charge_date, due_date)``;
the total is split across them with :func:`homeoa.billing.services.allocate` using equal
weights, so leftover cents (added or removed) land on the earliest installments and the amounts
sum exactly to the total. ``monthly_dates(first_due, count)`` builds monthly dates.

``post_installment(installment)`` splits the installment's amount across units (by
allocated interest, or equally for ``flat``) with the same ``allocate``, then posts per unit
one :class:`Charge` (``ChargeKind.SPECIAL``) and one ledger entry: Dr receivable (tagged
unit + owner of record on the charge date) / Cr income, both in the operating fund (
owner balances stay single-fund), ``source_type="billing.special"``.

Designated for reserves (``for_reserves``): each posted installment also posts one interfund
entry for its amount, the same shape as the monthly reserve contribution
(``source_type="billing.special_reserve"``), stored on ``installment.reserve_entry``:

* operating: Dr 5900 Reserve contributions / Cr 2900 Due to other fund
* reserve:   Dr 1900 Due from other fund   / Cr 4900 Reserve contributions received

The reserve fund and those accounts come from the chart (``default_schedule_kwargs``).
Posting is once per installment: the row is locked and ``posted_at`` set in the same
transaction as every entry; a second call returns ``False`` and writes nothing.

``post_due_installments(association, as_of)`` posts every unposted installment whose charge
date has been reached (periodic job ``billing.post_special_installments``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import rules
from homeoa.ledger.services import LedgerError, Line, post_entry
from homeoa.registry import services as registry

from ..latefee_models import SpecialMethod
from ..models import Charge, ChargeKind, SpecialAssessment, SpecialInstallment
from . import BillingError, _units, allocate


def monthly_dates(first_due: dt.date, count: int) -> list[tuple[dt.date, dt.date]]:
    """``count`` (charge_date, due_date) pairs a month apart, charged on the due date."""
    if count < 1:
        raise BillingError(_("Give at least one installment."))
    out = []
    for i in range(count):
        month = first_due.month - 1 + i
        year = first_due.year + month // 12
        day = min(first_due.day, 28)
        d = dt.date(year, month % 12 + 1, day)
        out.append((d, d))
    return out


def split_installments(total: Decimal, count: int) -> list[Decimal]:
    shares = allocate(total, [(i, Decimal(1)) for i in range(count)])
    return [shares[i] for i in range(count)]


def create_special(
    association,
    *,
    name: str,
    total: Decimal,
    installments: list[tuple[dt.date, dt.date]],
    operating_fund,
    receivable_account,
    income_account,
    method: str = SpecialMethod.ALLOCATED_INTEREST,
    description: str = "",
    created_by=None,
    for_reserves: bool = False,
) -> SpecialAssessment:
    total = Decimal(total)
    if total <= 0:
        raise BillingError(_("The total must be more than zero."))
    if not installments:
        raise BillingError(_("Give at least one installment."))
    amounts = split_installments(total, len(installments))
    if any(a <= 0 for a in amounts):
        raise BillingError(_("Each installment must be at least one cent."))
    actor = created_by if getattr(created_by, "pk", None) else None
    with transaction.atomic():
        sa = SpecialAssessment.objects.create(
            association=association,
            name=name,
            description=description,
            total=total,
            method=method,
            operating_fund=operating_fund,
            for_reserves=for_reserves,
            receivable_account=receivable_account,
            income_account=income_account,
            created_by=actor,
        )
        for n, ((charge_date, due_date), amount) in enumerate(
            zip(installments, amounts, strict=True), start=1
        ):
            SpecialInstallment.objects.create(
                association=association,
                special=sa,
                number=n,
                amount=amount,
                charge_date=charge_date,
                due_date=due_date,
            )
    return sa


def unit_amounts(special: SpecialAssessment, amount: Decimal) -> list[tuple]:
    """``[(unit, amount)]`` for one installment amount; raises on bad interests."""
    units = _units(special.association)
    if not units:
        raise BillingError(_("There are no units to bill."))
    if special.method == SpecialMethod.FLAT:
        shares = allocate(amount, [(u.pk, Decimal(1)) for u in units])
    else:
        if not registry.interest_total_ok(special.association):
            raise BillingError(
                _("Allocated interests total %(t)s%%, not 100%%. Fix them on the Units page.")
                % {"t": registry.interest_total(special.association)}
            )
        shares = allocate(amount, [(u.pk, u.allocated_interest) for u in units])
    return [(u, shares[u.pk]) for u in units if shares[u.pk] > 0]


def preview(special: SpecialAssessment) -> dict:
    """Per-unit amounts for every installment and per-unit totals (no writes)."""
    rows: dict[int, dict] = {}
    for inst in special.installments.all():
        for unit, amount in unit_amounts(special, inst.amount):
            row = rows.setdefault(unit.pk, {"unit": unit, "amounts": [], "total": Decimal(0)})
            row["amounts"].append(amount)
            row["total"] += amount
    return rows


def post_installment(installment: SpecialInstallment, *, posted_by=None, source="manual") -> bool:
    """Post one installment to every unit once. ``False`` when it was already posted."""
    actor = posted_by if getattr(posted_by, "pk", None) else None
    with transaction.atomic():
        inst = (
            SpecialInstallment.all_associations.select_for_update()
            .select_related("special")
            .get(pk=installment.pk)
        )
        if inst.posted:
            return False
        sa = inst.special
        if not may_post(sa):
            raise BillingError(
                _("Owners have not ratified this special assessment, so nothing can post yet.")
            )
        count = sa.installments.count()
        description = _("Special assessment · %(n)s · %(i)s of %(c)s") % {
            "n": sa.name,
            "i": inst.number,
            "c": count,
        }
        charges = []
        try:
            for unit, amount in unit_amounts(sa, inst.amount):
                op = registry.ownership_period_on(unit, inst.charge_date)
                if op is None:
                    raise BillingError(
                        _("Unit %(u)s has no owner of record on %(d)s.")
                        % {"u": unit.number, "d": inst.charge_date.isoformat()}
                    )
                entry = post_entry(
                    [
                        Line(
                            account=sa.receivable_account,
                            fund=sa.operating_fund,
                            debit=amount,
                            unit_id=unit.pk,
                            ownership_period_id=op.pk,
                        ),
                        Line(
                            account=sa.income_account,
                            fund=sa.operating_fund,
                            credit=amount,
                            unit_id=unit.pk,
                        ),
                    ],
                    date=inst.charge_date,
                    memo=_("%(d)s · Unit %(u)s") % {"d": description, "u": unit.number},
                    source_type="billing.special",
                    source_id=f"{inst.pk}:{unit.pk}",
                    posted_by=actor,
                    association=sa.association,
                )
                charges.append(
                    Charge.objects.create(
                        association=sa.association,
                        unit=unit,
                        ownership_period=op,
                        kind=ChargeKind.SPECIAL,
                        description=description[:200],
                        amount=amount,
                        open_balance=amount,
                        charge_date=inst.charge_date,
                        due_date=inst.due_date,
                        entry=entry,
                    )
                )
            if sa.for_reserves:
                inst.reserve_entry = post_entry(
                    _reserve_lines(sa, inst.amount),
                    date=inst.charge_date,
                    memo=_("Reserve transfer · %(d)s") % {"d": description},
                    source_type="billing.special_reserve",
                    source_id=inst.pk,
                    posted_by=actor,
                    association=sa.association,
                )
        except LedgerError as exc:
            raise BillingError(str(exc)) from exc
        inst.charges.add(*charges)
        inst.posted_at = timezone.now()
        inst.posted_by = actor
        inst.source = source
        inst.save(update_fields=["posted_at", "posted_by", "source", "reserve_entry"])
    return True


def _reserve_lines(sa: SpecialAssessment, amount: Decimal) -> list[Line]:
    from . import default_schedule_kwargs

    acc = default_schedule_kwargs(sa.association)
    needed = (
        "reserve_fund",
        "reserve_expense_account",
        "due_to_reserve_account",
        "due_from_operating_account",
        "reserve_income_account",
    )
    if any(acc[k] is None for k in needed):
        raise BillingError(
            _("The chart of accounts has no reserve fund or reserve transfer accounts.")
        )
    op, res = sa.operating_fund, acc["reserve_fund"]
    return [
        Line(account=acc["reserve_expense_account"], fund=op, debit=amount),
        Line(account=acc["due_to_reserve_account"], fund=op, credit=amount),
        Line(account=acc["due_from_operating_account"], fund=res, debit=amount),
        Line(account=acc["reserve_income_account"], fund=res, credit=amount),
    ]


def post_due_installments(association, as_of: dt.date, *, since: dt.date | None = None) -> int:
    """Post unposted installments charged on or before ``as_of`` (and on or after ``since``,
    when given: the scheduled job passes the go-live date)."""
    due = SpecialInstallment.all_associations.filter(
        association=association, posted_at__isnull=True, charge_date__lte=as_of
    )
    if since is not None:
        due = due.filter(charge_date__gte=since)
    due = due.select_related("special").order_by("charge_date", "pk")
    return sum(
        1 for inst in due if may_post(inst.special) and post_installment(inst, source="scheduled")
    )


# ---------------------------------------------------------------- ratification

RATIFICATION_KEY = "special_assessments.ratification_required"


def ratification_required(association) -> bool:
    return bool(rules.value(association, RATIFICATION_KEY))


def may_post(sa: SpecialAssessment) -> bool:
    if not ratification_required(sa.association):
        return sa.ratification != "rejected"
    return sa.ratification in ("ratified", "emergency")


# ------------------------------------------------------- emergency (WA: RCW 64.90.502(6))

EMERGENCY_KEY = "special_assessments.emergency_board_vote"
EMERGENCY_TYPE = "billing.special_emergency"


def emergency_rule(association):
    """The board vote the rule pack requires for an emergency special assessment, or a
    resolved rule with no value when the pack doesn't allow one."""
    return rules.resolve(association, EMERGENCY_KEY)


def vote_passes(needed: str, votes_for: int, seats: int) -> bool:
    if seats <= 0 or not 0 <= votes_for <= seats:
        return False
    if needed == "unanimous":
        return votes_for == seats
    if needed == "two_thirds":
        return votes_for * 3 >= seats * 2
    return votes_for * 2 > seats  # majority


def register_notification_type() -> None:
    from django.utils.translation import gettext_lazy

    from homeoa.notify import types as ntypes

    ntypes.register(
        ntypes.NotificationType(
            key=EMERGENCY_TYPE,
            label=gettext_lazy("Emergency special assessment"),
            audience=ntypes.AUDIENCE_RESIDENTS,
            subject_template=(
                "{% load i18n %}{% blocktranslate with n=name %}"
                "Emergency special assessment: {{ n }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=name t=total d=decided %}"
                "On {{ d }} the board adopted an emergency special assessment, {{ n }}, of "
                "{{ t }} in total, without owner ratification as the law allows in an "
                "emergency.{% endblocktranslate %}\n\n{{ reason }}\n\n{{ cite }}"
            ),
        )
    )


@transaction.atomic
def record_emergency(
    sa: SpecialAssessment,
    *,
    kind: str,
    reason: str,
    decided_on: dt.date,
    votes_for: int,
    seats: int,
    user=None,
) -> SpecialAssessment:
    """Make the assessment effective on the board's emergency vote and tell the owners."""
    from homeoa import audit
    from homeoa.ledger.templatetags.ledger_tags import money
    from homeoa.notify.services import notify_after_commit

    rule = emergency_rule(sa.association)
    if not rule.value:
        raise BillingError(
            _("The rule pack does not allow a special assessment without owner ratification.")
        )
    if sa.installments.filter(posted_at__isnull=False).exists():
        raise BillingError(_("Installments have already posted."))
    if sa.ratification:
        raise BillingError(_("The owners' decision is already recorded."))
    valid_kinds = {k for k, _label in SpecialAssessment._meta.get_field("emergency_kind").choices}
    reason = (reason or "").strip()
    if kind not in valid_kinds or not reason:
        raise BillingError(_("Choose the kind of emergency and say what happened."))
    if not vote_passes(rule.value, votes_for, seats):
        raise BillingError(
            _("The vote does not meet the %(needed)s board vote the law requires (%(cite)s).")
            % {"needed": str(rule.value).replace("_", "-"), "cite": rule.citation}
        )
    sa.emergency_kind = kind
    sa.emergency_reason = reason
    sa.emergency_decided_on = decided_on
    sa.emergency_votes_for = votes_for
    sa.emergency_board_size = seats
    sa.ratification = "emergency"
    sa.save()
    audit.record(
        "billing.special_emergency",
        sa,
        actor=user,
        changes={"kind": kind, "votes_for": votes_for, "seats": seats},
    )
    context = {
        "name": sa.name,
        "total": money(sa.total),
        "decided": decided_on.isoformat(),
        "reason": reason,
        "cite": rule.citation,
    }
    for unit in _units(sa.association):
        owners = registry.current_owners(unit, decided_on)
        if owners:
            notify_after_commit(
                EMERGENCY_TYPE,
                owners,
                context,
                decided_on,
                obj=sa,
                association=sa.association,
                unit=unit,
            )
    return sa


def ratification_window(sa: SpecialAssessment) -> tuple[dt.date, dt.date] | None:
    """Earliest and latest meeting dates after the summary was sent (the budget's window)."""
    if sa.summary_sent_on is None:
        return None
    low = rules.value(sa.association, "budget.ratification.min_days")
    high = rules.value(sa.association, "budget.ratification.max_days")
    if low is None or high is None:
        return None
    return (
        sa.summary_sent_on + dt.timedelta(days=int(low)),
        sa.summary_sent_on + dt.timedelta(days=int(high)),
    )


@transaction.atomic
def record_ratification(
    sa: SpecialAssessment, *, summary_sent_on: dt.date, meeting_on: dt.date, outcome: str
) -> SpecialAssessment:
    """Record the owners' decision. The meeting must fall in the rule pack's window."""
    if outcome not in ("ratified", "rejected"):
        raise BillingError(_("Choose ratified or rejected."))
    if sa.installments.filter(posted_at__isnull=False).exists():
        raise BillingError(_("Installments have already posted."))
    sa.summary_sent_on = summary_sent_on
    window = ratification_window(sa)
    if window and not (window[0] <= meeting_on <= window[1]):
        raise BillingError(
            _(
                "The ratification meeting must be between %(start)s and %(end)s: %(low)s to "
                "%(high)s days after the summary was sent."
            )
            % {
                "start": window[0].isoformat(),
                "end": window[1].isoformat(),
                "low": (window[0] - summary_sent_on).days,
                "high": (window[1] - summary_sent_on).days,
            }
        )
    sa.ratification_meeting_on = meeting_on
    sa.ratification = outcome
    sa.save(update_fields=["summary_sent_on", "ratification_meeting_on", "ratification"])
    return sa


def special_income_account(association):
    """4050 Special assessment income, created for associations whose chart predates it."""
    from homeoa.ledger.chart import ACCOUNTS
    from homeoa.ledger.models import Account

    code, name, type_, sub = next(row for row in ACCOUNTS if row[0] == "4050")
    account, _created = Account.all_associations.get_or_create(
        association=association,
        code=code,
        defaults={"name": name, "type": type_, "in_unit_subledger": sub},
    )
    return account


def default_special_kwargs(association) -> dict:
    from homeoa.ledger.models import Account, Fund

    accounts = Account.all_associations.filter(association=association)
    funds = Fund.all_associations.filter(association=association)
    return {
        "receivable_account": accounts.filter(code="1100").first(),
        "income_account": special_income_account(association),
        "operating_fund": funds.filter(code="operating").first(),
    }
