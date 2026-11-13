"""Rule-pack building blocks: the key catalog types, ``RuleValue`` and ``RulePack``.

Importable without Django models (plugin manifests import it).
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from decimal import Decimal, InvalidOperation
from typing import Any


class Kind(enum.StrEnum):
    DAYS = "days"  # int >= 0
    MONTHS = "months"  # int >= 0
    MINUTES = "minutes"  # int >= 0
    COUNT = "count"  # int >= 0 (people, approvals)
    MONEY = "money"  # Decimal >= 0, 2 places
    PERCENT = "percent"  # Decimal 0..100
    BOOL = "bool"
    ORDER = "order"  # list of choices, each used once
    STEPS = "steps"  # list of choices, in order
    CHOICE = "choice"  # exactly one of choices


class Basis(enum.StrEnum):
    STATUTE = "statute"  # claimed to come from law (must cite; verified only when checked)
    POLICY = "policy"  # an association policy default (e.g. payment allocation order), not statute
    BOARD = "board"  # not set; the board decides
    ASSOCIATION = "association"  # an association-level override


@dataclasses.dataclass(frozen=True)
class RuleKey:
    """A known rule key: its type, label and allowed choices (for ORDER/STEPS)."""

    key: str
    kind: Kind
    label: Any
    help: Any = ""
    choices: tuple[str, ...] = ()
    # The key whose pack value (in force) is a maximum for this key's association
    # override. May be the key itself: the pack value is a statutory ceiling.
    cap_key: str = ""

    def coerce(self, raw: Any) -> Any:
        """Validate and convert a raw (e.g. JSON) value. ``None`` means "not set"."""
        if raw is None:
            return None
        match self.kind:
            case Kind.DAYS | Kind.MONTHS | Kind.MINUTES | Kind.COUNT:
                unit = {Kind.DAYS: "days", Kind.MONTHS: "months", Kind.MINUTES: "minutes"}.get(
                    self.kind, "people"
                )
                if isinstance(raw, bool):
                    raise ValueError(f"{self.key}: expected a number of {unit}")
                try:
                    value = int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{self.key}: expected a number of {unit}") from exc
                if value < 0:
                    raise ValueError(f"{self.key}: must not be negative")
                return value
            case Kind.MONEY | Kind.PERCENT:
                if isinstance(raw, (bool, float)):
                    raise ValueError(f"{self.key}: use a decimal string, not {type(raw).__name__}")
                try:
                    value = Decimal(str(raw))
                except InvalidOperation as exc:
                    raise ValueError(f"{self.key}: expected a decimal") from exc
                if not value.is_finite() or value < 0:
                    raise ValueError(f"{self.key}: must be zero or more")
                if self.kind is Kind.PERCENT and value > 100:
                    raise ValueError(f"{self.key}: must be at most 100")
                return value.quantize(Decimal("0.01")) if self.kind is Kind.MONEY else value
            case Kind.BOOL:
                if not isinstance(raw, bool):
                    raise ValueError(f"{self.key}: expected true or false")
                return raw
            case Kind.CHOICE:
                if not isinstance(raw, str) or raw not in self.choices:
                    raise ValueError(f"{self.key}: must be one of {list(self.choices)}")
                return raw
            case Kind.ORDER | Kind.STEPS:
                if isinstance(raw, str) or not hasattr(raw, "__iter__"):
                    raise ValueError(f"{self.key}: expected a list")
                items = [str(x) for x in raw]
                bad = [x for x in items if x not in self.choices]
                if bad:
                    raise ValueError(f"{self.key}: unknown items {bad}")
                if len(set(items)) != len(items):
                    raise ValueError(f"{self.key}: items repeat")
                if self.kind is Kind.ORDER and set(items) != set(self.choices):
                    raise ValueError(f"{self.key}: must list each of {list(self.choices)} once")
                return tuple(items)
        raise ValueError(f"{self.key}: unknown kind {self.kind}")  # pragma: no cover

    def to_json(self, value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple):
            return list(value)
        return value


@dataclasses.dataclass(frozen=True, kw_only=True)
class RuleValue:
    """One value a pack declares, in force from ``effective_from`` to ``effective_to``.

    Either date may be ``None`` (open). ``verified`` is True only after the citation was
    checked against current law: it needs ``citation``, ``quote`` and
    ``checked_on``. ``attorney_review`` marks values that also need an attorney.
    ``value=None`` means the pack knows the rule exists but not the value (falls back).
    """

    value: Any
    basis: Basis
    verified: bool = False
    citation: str = ""
    note: str = ""
    quote: str = ""
    source_url: str = ""
    checked_on: datetime.date | None = None
    attorney_review: bool = False
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None  # inclusive

    def in_force(self, on: datetime.date) -> bool:
        return (self.effective_from is None or self.effective_from <= on) and (
            self.effective_to is None or on <= self.effective_to
        )


def _overlap(a: RuleValue, b: RuleValue) -> bool:
    lo = datetime.date.min
    hi = datetime.date.max
    return (a.effective_from or lo) <= (b.effective_to or hi) and (b.effective_from or lo) <= (
        a.effective_to or hi
    )


class RulePackError(ValueError):
    """A pack is malformed (overlapping ranges, verified without a citation, ...)."""


# ---------------------------------------------------------------- setup wizard


@dataclasses.dataclass(frozen=True, kw_only=True)
class SetupChoice:
    """One answer the board can pick. ``value`` is stored; ``explain`` says why."""

    value: Any
    label: Any
    explain: Any = ""


class _Unset:
    def __repr__(self):
        return "NOT_SET"

    def __bool__(self):
        return False


NOT_SET = _Unset()


@dataclasses.dataclass(frozen=True, kw_only=True)
class LegalLimit:
    """How the law shapes a setup answer: exactly one of

    - ``fixed``: the value the law sets; the question is shown answered, not asked;
    - ``allowed``: the choice values the law permits; the others are refused;
    - ``minimum`` / ``maximum`` (either may be absent): a number, or ``"rule:<key>"`` for
      the pack's own value of that rule in force on the association's today. A rule with
      no value in force does not bind.

    ``citation`` may be empty when the bounds come from rules; theirs is used then.
    """

    citation: str = ""
    source_url: str = ""
    verified: bool = False
    attorney_review: bool = False
    note: Any = ""
    fixed: Any = NOT_SET
    allowed: tuple | None = None
    minimum: Any = None
    maximum: Any = None

    def __post_init__(self):
        if self.allowed is not None:
            object.__setattr__(self, "allowed", tuple(self.allowed))
        kinds = [self.is_fixed, self.allowed is not None, self.is_range]
        if sum(kinds) != 1:
            raise RulePackError(
                "A legal limit needs exactly one of fixed, allowed, or minimum/maximum."
            )
        lo, hi = self.minimum, self.maximum
        plain = [b for b in (lo, hi) if b is not None and not isinstance(b, str)]
        if len(plain) == 2 and Decimal(str(lo)) > Decimal(str(hi)):
            raise RulePackError("A legal limit's minimum is above its maximum.")
        for b in (lo, hi):
            if isinstance(b, str) and not b.startswith("rule:"):
                raise RulePackError(f'A legal bound must be a number or "rule:<key>", not {b!r}.')

    @property
    def is_fixed(self) -> bool:
        return self.fixed is not NOT_SET

    @property
    def is_range(self) -> bool:
        return self.minimum is not None or self.maximum is not None


@dataclasses.dataclass(frozen=True, kw_only=True)
class SetupQuestion:
    """A policy question in the association setup wizard (``homeoa.setup``).

    ``target`` names what the answer sets; core knows how to apply each target
    (``homeoa.setup.targets``): ``rule:<key>`` sets a rule override, the others create
    a late fee policy, a fine schedule, approval rules and so on. ``kind`` is one of
    choice, money, days, percent, bool. ``other`` lets the board type a number that is
    not one of the choices. ``citation`` and ``note`` say where the suggestion comes from.
    ``module`` is the optional module the question is about (``None``: core); the wizard
    leaves it out while that module is off. ``law`` is the legal constraint on the answer
    (``LegalLimit``), if any.
    """

    target: str
    label: Any
    kind: str
    choices: tuple[SetupChoice, ...] = ()
    suggested: Any = None
    other: bool = False
    help: Any = ""
    citation: str = ""
    source_url: str = ""
    note: Any = ""
    attorney_review: bool = False
    module: str | None = None
    law: LegalLimit | None = None

    def __post_init__(self):
        object.__setattr__(self, "choices", tuple(self.choices))


@dataclasses.dataclass(frozen=True, kw_only=True)
class SetupSection:
    """One page of the wizard; adopting it is one board resolution. ``module`` is the
    optional module the whole section is about (``None``: core)."""

    key: str
    title: Any
    intro: Any = ""
    questions: tuple[SetupQuestion, ...] = ()
    module: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "questions", tuple(self.questions))


@dataclasses.dataclass(frozen=True, kw_only=True)
class RequiredFeature:
    """A core feature the law requires, shown as always on. ``citation`` is empty when
    the pack has none for it."""

    id: str
    label: Any
    citation: str = ""
    verified: bool = False


@dataclasses.dataclass(frozen=True, kw_only=True)
class RulePack:
    """A jurisdiction's rule values. ``values[key]`` is one ``RuleValue`` or a sequence
    of them with non-overlapping effective ranges. Validated on construction."""

    id: str
    name: Any
    jurisdiction: str  # "" for generic, e.g. "US-WA"
    values: dict[str, Any]
    description: Any = ""
    # Recurring legal obligations; the compliance app turns them into periods.
    obligations: tuple[Obligation, ...] = ()
    # The association setup wizard's sections (questions, suggestions, citations).
    setup: tuple[SetupSection, ...] = ()
    # Optional modules the law requires: module id -> citation.
    required_modules: dict[str, str] = dataclasses.field(default_factory=dict)
    # Always-on core features the law requires, for the Features screen.
    required_features: tuple[RequiredFeature, ...] = ()
    # The sections that decide which law applies to the association (e.g. when a newer act
    # takes over from an older one), as a citation; the yearly law check watches them too.
    applies_under: str = ""
    # The last date the pack's law was checked through (``None``: never checked).
    law_checked_through: datetime.date | None = None
    # Dotted path to the pack's law change source (``homeoa.rules.lawwatch``); empty
    # means the law behind the pack cannot be checked automatically.
    change_source: str = ""

    def __post_init__(self):
        object.__setattr__(self, "required_features", tuple(self.required_features))
        object.__setattr__(self, "obligations", tuple(self.obligations))
        object.__setattr__(self, "setup", tuple(self.setup))
        _check_obligations(self.id, self.obligations)
        normalized: dict[str, tuple[RuleValue, ...]] = {}
        for key, raw in self.values.items():
            items = (raw,) if isinstance(raw, RuleValue) else tuple(raw)
            for item in items:
                if not isinstance(item, RuleValue):
                    raise RulePackError(f"{self.id}: {key}: expected RuleValue")
                if (
                    item.effective_from
                    and item.effective_to
                    and item.effective_to < item.effective_from
                ):
                    raise RulePackError(f"{self.id}: {key}: range ends before it starts")
                if item.verified and not (item.citation and item.quote and item.checked_on):
                    raise RulePackError(
                        f"{self.id}: {key}: verified values need a citation, quote and "
                        "checked_on date"
                    )
            for i, a in enumerate(items):
                for b in items[i + 1 :]:
                    if _overlap(a, b):
                        raise RulePackError(f"{self.id}: {key}: effective ranges overlap")
            normalized[key] = tuple(
                sorted(items, key=lambda v: v.effective_from or datetime.date.min)
            )
        object.__setattr__(self, "values", normalized)

    def schedule(self, key: str) -> tuple[RuleValue, ...]:
        """Every value for ``key``, oldest first."""
        return self.values.get(key, ())

    def get(self, key: str, as_of: datetime.date | None = None) -> RuleValue | None:
        """The value in force on ``as_of`` (default: today, server date)."""
        on = as_of or datetime.date.today()
        for item in self.values.get(key, ()):
            if item.in_force(on):
                return item
        return None

    def next_value(self, key: str, as_of: datetime.date) -> RuleValue | None:
        """The first value that starts after ``as_of``, if any."""
        for item in self.values.get(key, ()):
            if item.effective_from is not None and item.effective_from > as_of:
                return item
        return None


# ---------------------------------------------------------------- obligations


class Frequency(enum.StrEnum):
    ANNUAL = "annual"  # one period per calendar year
    EVERY_N_YEARS = "every_n_years"  # one period every ``every_years`` after the last done


@dataclasses.dataclass(frozen=True, kw_only=True)
class ObligationPath:
    """One way to meet an obligation in a period (e.g. "audit" or "waiver vote").

    ``threshold_key`` names a rule key whose value in force on the due date is shown with
    the path (e.g. the waiver vote percentage)."""

    id: str
    label: Any
    help: Any = ""
    citation: str = ""
    source_url: str = ""
    threshold_key: str = ""


@dataclasses.dataclass(frozen=True, kw_only=True)
class ObligationExemption:
    """A yes/no question; "yes" exempts the association for that period."""

    id: str
    question: Any
    citation: str = ""
    source_url: str = ""
    note: str = ""
    attorney_review: bool = False


@dataclasses.dataclass(frozen=True, kw_only=True)
class Obligation:
    """A recurring obligation, in force from ``effective_from`` to ``effective_to``.

    Several ``Obligation`` may share an ``id`` with non-overlapping ranges; the one
    in force on a period's due date applies. A period for year ``Y`` is due on
    ``due_month``/``due_day`` of ``Y``, or when those are ``None`` on the last day of the
    association's fiscal year ending in ``Y``, and opens ``opens_days_before`` days
    earlier.
    """

    id: str
    title: Any
    description: Any = ""
    citation: str = ""
    source_url: str = ""
    frequency: Frequency = Frequency.ANNUAL
    every_years: int = 1
    due_month: int | None = None  # None: the fiscal year end
    due_day: int | None = None
    opens_days_before: int = 90
    paths: tuple[ObligationPath, ...] = ()
    exemptions: tuple[ObligationExemption, ...] = ()
    verified: bool = False
    attorney_review: bool = False
    note: str = ""
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None  # inclusive

    def in_force(self, on: datetime.date) -> bool:
        return (self.effective_from is None or self.effective_from <= on) and (
            self.effective_to is None or on <= self.effective_to
        )

    def due_date(self, year: int, fiscal_start_month: int = 1) -> datetime.date:
        if self.due_month is not None:
            return datetime.date(year, self.due_month, self.due_day or 1)
        return fiscal_year_end(year, fiscal_start_month)

    def opens_on(self, year: int, fiscal_start_month: int = 1) -> datetime.date:
        return self.due_date(year, fiscal_start_month) - datetime.timedelta(
            days=self.opens_days_before
        )

    def path(self, path_id: str) -> ObligationPath | None:
        return next((p for p in self.paths if p.id == path_id), None)

    def exemption(self, exemption_id: str) -> ObligationExemption | None:
        return next((e for e in self.exemptions if e.id == exemption_id), None)


def fiscal_year_end(year: int, fiscal_start_month: int = 1) -> datetime.date:
    """The last day of the fiscal year that ends in ``year``."""
    if fiscal_start_month == 1:
        return datetime.date(year, 12, 31)
    return datetime.date(year, fiscal_start_month, 1) - datetime.timedelta(days=1)


def _check_obligations(pack_id: str, obligations: tuple[Obligation, ...]) -> None:
    by_id: dict[str, list[Obligation]] = {}
    for ob in obligations:
        if not isinstance(ob, Obligation):
            raise RulePackError(f"{pack_id}: obligations: expected Obligation")
        if not ob.paths:
            raise RulePackError(f"{pack_id}: obligation {ob.id}: needs at least one path")
        ids = [p.id for p in ob.paths] + [e.id for e in ob.exemptions]
        if len(set(ids)) != len(ids):
            raise RulePackError(f"{pack_id}: obligation {ob.id}: path/exemption ids repeat")
        if ob.every_years < 1:
            raise RulePackError(f"{pack_id}: obligation {ob.id}: every_years must be >= 1")
        if ob.verified and not ob.citation:
            raise RulePackError(f"{pack_id}: obligation {ob.id}: verified needs a citation")
        try:
            ob.due_date(2028)  # a leap year accepts Feb 29
        except ValueError as exc:
            raise RulePackError(f"{pack_id}: obligation {ob.id}: bad due date") from exc
        by_id.setdefault(ob.id, []).append(ob)
    for ob_id, items in by_id.items():
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                if _overlap(a, b):
                    raise RulePackError(f"{pack_id}: obligation {ob_id}: ranges overlap")
