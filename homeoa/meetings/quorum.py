"""Live quorum from attendance and proxies (GOV-05).

Who counts
----------
* Owners meetings: each unit counts once if it attended (in person or remote) or gave an
  active proxy. A proxy for a unit that also attended is ignored; a revoked proxy never
  counts. Weight per unit follows the association's vote weighting: one per unit,
  allocated interest, or custom weight, always ``Decimal``.
* Board meetings: each board member in office on the meeting date counts once if present.

The requirement
---------------
Association setting ``meeting_quorum`` (from the bylaws), per meeting kind:
``{"<kind>": {"basis": "percent" | "count", "value": "<number>"}}``. ``percent`` is a
percent of all votes (weighted); ``count`` is a number of units (or board members)
present. When unset, owners meetings fall back to the rule-pack key
``meetings.owner.quorum_percent``; when that is unset too, there is no quorum
requirement. No statute value lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext as _

from homeoa import audit, rules
from homeoa.governance import services as gov
from homeoa.registry import services as registry
from homeoa.registry.models import Unit

from .models import Attendance, Meeting, Proxy

SETTING = "meeting_quorum"
PACK_KEY = "meetings.owner.quorum_percent"
PERCENT = "percent"
COUNT = "count"
BASES = (PERCENT, COUNT)
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class Requirement:
    basis: str  # PERCENT, COUNT, or "" (none)
    value: Decimal | None
    source: str  # "setting", "pack", "none"
    rule: rules.Resolved | None = None  # the pack rule, for the verified badge

    @property
    def is_set(self) -> bool:
        return bool(self.basis) and self.value is not None


@dataclass(frozen=True)
class Quorum:
    requirement: Requirement
    total_weight: Decimal
    present_weight: Decimal
    total_count: int
    present_count: int
    in_person: int
    remote: int
    by_proxy: int
    ignored_proxies: list = field(default_factory=list)  # proxies of units present

    @property
    def percent(self) -> Decimal:
        if not self.total_weight:
            return Decimal("0")
        return (self.present_weight * HUNDRED / self.total_weight).quantize(Decimal("0.01"))

    @property
    def needed_count(self) -> Decimal | None:
        return self.requirement.value if self.requirement.basis == COUNT else None

    @property
    def met(self) -> bool:
        r = self.requirement
        if not r.is_set:
            return True
        if r.basis == COUNT:
            return Decimal(self.present_count) >= r.value
        if not self.total_weight:
            return False
        return self.present_weight * HUNDRED >= r.value * self.total_weight


def _parse(raw) -> tuple[str, Decimal | None]:
    if not isinstance(raw, dict) or raw.get("basis") not in BASES:
        return "", None
    try:
        value = Decimal(str(raw.get("value")))
    except InvalidOperation, TypeError:
        return "", None
    if value < 0 or (raw["basis"] == PERCENT and value > HUNDRED):
        return "", None
    return raw["basis"], value


def setting_for(association, kind: str) -> tuple[str, Decimal | None]:
    return _parse(((association.settings or {}).get(SETTING) or {}).get(kind))


def requirement(meeting: Meeting) -> Requirement:
    basis, value = setting_for(meeting.association, meeting.kind)
    if basis:
        return Requirement(basis, value, "setting")
    if meeting.is_owner_meeting:
        resolved = rules.resolve(meeting.association, PACK_KEY, meeting.date)
        if resolved.value is not None:
            return Requirement(PERCENT, Decimal(str(resolved.value)), "pack", resolved)
    return Requirement("", None, "none")


def set_requirement(association, kind: str, basis: str, value, *, user=None) -> None:
    """Store the bylaws quorum for a meeting kind; an empty ``basis`` clears it."""
    if kind not in Meeting.Kind.values:
        raise ValueError(_("Choose a meeting kind."))
    data = dict(association.settings or {})
    quorum = dict(data.get(SETTING) or {})
    old = quorum.get(kind)
    if not basis:
        quorum.pop(kind, None)
    else:
        parsed = _parse({"basis": basis, "value": value})
        if not parsed[0]:
            raise ValueError(_("Enter a quorum between 0 and 100 percent, or a count."))
        quorum[kind] = {"basis": basis, "value": str(parsed[1])}
    data[SETTING] = quorum
    association.settings = data
    association.save(update_fields=["settings"])
    audit.record(
        "meetings.quorum_setting",
        association,
        actor=user,
        changes={kind: [old, quorum.get(kind)]},
    )


def board_members(meeting: Meeting) -> list:
    """People on the board on the meeting date, each once."""
    seen, people = set(), []
    for term in gov.roster(meeting.association, meeting.date):
        if term.person_id not in seen:
            seen.add(term.person_id)
            people.append(term.person)
    return people


def compute(meeting: Meeting) -> Quorum:
    req = requirement(meeting)
    rows = list(Attendance.all_associations.filter(meeting=meeting))
    if not meeting.is_owner_meeting:
        members = {p.pk for p in board_members(meeting)}
        present = [r for r in rows if r.person_id in members]
        n = len(present)
        return Quorum(
            requirement=req,
            total_weight=Decimal(len(members)),
            present_weight=Decimal(n),
            total_count=len(members),
            present_count=n,
            in_person=sum(1 for r in present if r.mode == Attendance.Mode.IN_PERSON),
            remote=sum(1 for r in present if r.mode == Attendance.Mode.REMOTE),
            by_proxy=0,
        )
    units = {u.pk: u for u in Unit.all_associations.filter(association=meeting.association)}
    weights = {pk: registry.vote_weight(u) for pk, u in units.items()}
    attended = {r.unit_id: r.mode for r in rows if r.unit_id in units}
    proxies = list(
        Proxy.all_associations.filter(meeting=meeting, revoked_on__isnull=True).select_related(
            "unit"
        )
    )
    proxied = {p.unit_id for p in proxies if p.unit_id in units and p.unit_id not in attended}
    ignored = [p for p in proxies if p.unit_id in attended]
    present = set(attended) | proxied
    return Quorum(
        requirement=req,
        total_weight=sum(weights.values(), Decimal("0")),
        present_weight=sum((weights[pk] for pk in present), Decimal("0")),
        total_count=len(units),
        present_count=len(present),
        in_person=sum(1 for m in attended.values() if m == Attendance.Mode.IN_PERSON),
        remote=sum(1 for m in attended.values() if m == Attendance.Mode.REMOTE),
        by_proxy=len(proxied),
        ignored_proxies=ignored,
    )
