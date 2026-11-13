"""Cutover readiness (FIN-18): the checks the board reviews before leaving its management company.

Read-only: it reports on the import, the parallel-run diffs, the books lock and the people who
must be able to log in. Platform items (host, backups, SMTP) are manual; see deploy/RUNBOOK.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.utils.translation import gettext as _

from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.ledger.services import get_locked_through

from .diff.models import DiffRun
from .models import ImportRun


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    detail: str
    items: list[str] = field(default_factory=list)


SETTINGS_KEY = "cutover"
DEFAULT_CLEAN_MONTHS = 2  # Parallel run for 2-3 months
# Shown as a label only; the citation is unverified.
RECORDS_CITATION = "RCW 64.34.372(11)"


def _cfg(association) -> dict:
    data = (association.settings or {}).get(SETTINGS_KEY) or {}
    return data if isinstance(data, dict) else {}


def clean_months_required(association) -> int:
    try:
        n = int(_cfg(association).get("clean_months_required", DEFAULT_CLEAN_MONTHS))
    except TypeError, ValueError:
        return DEFAULT_CLEAN_MONTHS
    return max(n, 1)


def records_requested(association) -> dict | None:
    return _cfg(association).get("records_requested") or None


def _save(association, key, value, user, message):
    from homeoa.audit.services import record

    data = dict(association.settings or {})
    cfg = dict(_cfg(association))
    before = cfg.get(key)
    if value is None:
        cfg.pop(key, None)
    else:
        cfg[key] = value
    data[SETTINGS_KEY] = cfg
    association.settings = data
    association.save(update_fields=["settings"])
    record(
        "update",
        association,
        association=association,
        actor=user,
        changes={f"cutover.{key}": [before, value]},
        message=message,
    )


def set_clean_months_required(association, n: int, user=None) -> None:
    if int(n) < 1:
        raise ValueError("clean months must be at least 1")
    _save(association, "clean_months_required", int(n), user, "Cutover: clean months required")


def set_records_requested(association, requested: bool, user=None, on=None) -> None:
    value = None
    if requested:
        on = on or association_today(association)
        value = {"on": on.isoformat(), "by": getattr(user, "email", "") or ""}
    label = "Cutover: management company records delivery"
    _save(association, "records_requested", value, user, label)


@dataclass
class MonthStatus:
    month: dt.date
    status: str  # clean / differences / missing
    run: DiffRun | None


@dataclass
class Owners:
    total: int
    invited: int
    accepted: int
    consent: int


@dataclass
class Readiness:
    checks: list[Check]
    latest_diff: DiffRun | None
    clean_months: int
    residents_on: bool
    required_months: int = DEFAULT_CLEAN_MONTHS
    months: list[MonthStatus] = field(default_factory=list)
    open_todos: int = 0
    owners: Owners | None = None
    unverified_rules: int = 0
    records: dict | None = None

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks)


def office_holders(association, on: dt.date) -> list:
    """Current board members and other office holders (such as design approval reviewers),
    one entry per person."""
    from homeoa.governance import services as gov
    from homeoa.plugins import slots

    people = [t.person for t in gov.roster(association, on)]
    for fn in slots.values(slots.OFFICE_HOLDERS, association):
        people += fn(association, on)
    seen, out = set(), []
    for p in people:
        if p.pk not in seen:
            seen.add(p.pk)
            out.append(p)
    return out


def _month(d: dt.date) -> dt.date:
    return d.replace(day=1)


def month_statuses(runs: list[DiffRun], start: dt.date | None, today: dt.date):
    """One row per month from ``start`` (or the first run) to this month, newest first."""
    latest: dict[dt.date, DiffRun] = {}
    for r in runs:  # runs are newest first
        latest.setdefault(_month(r.as_of), r)
    candidates = list(latest) + ([_month(start)] if start else [])
    if not candidates:
        return []
    first = min(candidates)
    out, m, end = [], first, _month(today)
    while m <= end:
        r = latest.get(m)
        status = "missing" if r is None else ("clean" if r.is_clean else "differences")
        out.append(MonthStatus(m, status, r))
        m = (m + dt.timedelta(days=32)).replace(day=1)
    return out[::-1]


def owner_counts(association, on: dt.date) -> Owners:
    from homeoa.access.models import Invitation
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_notice_preference, current_owners

    people = {}
    for unit in Unit.all_associations.filter(association=association):
        for p in current_owners(unit, on):
            people[p.pk] = p
    invited = set(
        Invitation.all_associations.filter(
            association=association, person_id__in=people, status=Invitation.Status.SENT
        ).values_list("person_id", flat=True)
    )
    consent = 0
    for p in people.values():
        pref = current_notice_preference(p)
        if pref is not None and pref.valid_consent:
            consent += 1
    return Owners(
        total=len(people),
        invited=len(invited),
        accepted=sum(1 for p in people.values() if p.user_id),
        consent=consent,
    )


def unverified_rule_count(association, on: dt.date) -> int:
    from homeoa import rules
    from homeoa.rules.base import Basis

    return sum(
        1 for r in rules.all_values(association, on) if r.basis == Basis.STATUTE and not r.verified
    )


def readiness(association, today: dt.date | None = None) -> Readiness:
    today = today or association_today(association)
    checks: list[Check] = []

    opening = (
        ImportRun.all_associations.filter(
            association=association,
            mode=ImportRun.Mode.COMMIT,
            status=ImportRun.Status.COMMITTED,
        )
        .order_by("as_of", "pk")
        .first()
    )
    checks.append(
        Check(
            "import",
            _("Opening data imported"),
            opening is not None,
            _("Imported as of %(d)s.") % {"d": opening.as_of.isoformat()}
            if opening
            else _("No import has been committed yet."),
        )
    )

    locked = get_locked_through(association)
    checks.append(
        Check(
            "lock",
            _("Opening-balance month closed"),
            opening is not None and locked is not None and locked >= opening.as_of,
            _("Books locked through %(d)s.") % {"d": locked.isoformat()}
            if locked
            else _("The books are not locked yet. Close the opening-balance month."),
        )
    )

    runs = list(DiffRun.all_associations.filter(association=association).order_by("-as_of", "-pk"))
    latest = runs[0] if runs else None
    clean_months = len({_month(r.as_of) for r in runs if r.is_clean})
    required = clean_months_required(association)
    if latest is None:
        detail = _("No parallel-run comparison yet.")
    elif latest.is_clean:
        detail = _("The latest comparison (%(d)s) matches to the cent.") % {
            "d": latest.as_of.isoformat()
        }
    else:
        detail = _("The latest comparison (%(d)s) has %(n)s unexplained differences.") % {
            "d": latest.as_of.isoformat(),
            "n": latest.open_differences().count(),
        }
    checks.append(
        Check(
            "parallel",
            _("Latest parallel run is clean"),
            latest is not None and latest.is_clean,
            detail,
        )
    )
    checks.append(
        Check(
            "months",
            _("Enough clean months"),
            clean_months >= required,
            _("%(n)s of %(m)s clean months.") % {"n": clean_months, "m": required},
        )
    )

    holders = office_holders(association, today)
    missing = []
    for p in holders:
        gaps = []
        if p.user_id is None:
            gaps.append(_("no login"))
        if not p.email:
            gaps.append(_("no email"))
        if gaps:
            missing.append(f"{p.full_name}: {', '.join(gaps)}")
    checks.append(
        Check(
            "people",
            _("Every board member and ARC reviewer has a login and an email"),
            bool(holders) and not missing,
            _("%(n)s people checked.") % {"n": len(holders)}
            if holders
            else _("No current board terms are recorded."),
            missing,
        )
    )

    from homeoa.dashboard.todos import collect

    records = records_requested(association)
    checks.append(
        Check(
            "records",
            _("Records delivery requested from the management company (%(c)s, unverified)")
            % {"c": RECORDS_CITATION},
            records is not None,
            _("Requested on %(d)s.") % {"d": records["on"]} if records else _("Not yet recorded."),
        )
    )

    return Readiness(
        checks,
        latest,
        clean_months,
        residents_enabled(association),
        required_months=required,
        months=month_statuses(runs, opening.as_of if opening else None, today),
        open_todos=len(collect(association)),
        owners=owner_counts(association, today),
        unverified_rules=unverified_rule_count(association, today),
        records=records,
    )
