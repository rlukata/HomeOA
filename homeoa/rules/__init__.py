"""Rule packs (GOV-09 framework): jurisdiction rules as data, never hardcoded in core.

Python API (``from homeoa import rules``)
-----------------------------------------
``rules.value(association, key, as_of=None)``    the value in force (``None`` = not set).
``rules.resolve(association, key, as_of=None)``  a ``Resolved`` with value, verified,
                                       citation, quote, checked date, basis, source,
                                       the next scheduled value and cap clamping.
``rules.all_values(association)``      ``Resolved`` for every key in ``rules.keys.KEYS``.
``rules.active_pack(association)``     the association's ``RulePack``.
``rules.set_override(association, key, value)`` / ``rules.clear_override(...)``
                                       association-level overrides (audited).
Typed accessors: ``payment_allocation_order``, ``late_fee_cap``,
``notice_window(association, kind) -> (min_days, max_days)``, ``fine_due_process_steps``,
``vote_threshold(association, kind)``, ``delinquency_offsets(association)``.

Resolution
-----------------
``as_of`` defaults to ``association_today``. A pack may give several ``RuleValue`` per
key with non-overlapping ``effective_from``/``effective_to`` ranges; the one in force
on ``as_of`` is used.

1. An association override in ``Association.settings["rules"][key]``. When the key has a
   ``cap_key`` and the active pack's cap in force is lower, the override is clamped to
   the cap (``Resolved.clamped``). Pack values are trusted and never clamped.
2. The active pack's value, when not ``None``. The active pack is the ``RulePack``
   provider of an enabled plugin (``homeoa.providers``), e.g. ``homeoa-rulepack-us-wa``;
   without one it is the generic pack.
3. The generic pack's value.

``verified=True`` needs a citation, quote and ``checked_on``, enforced when the
pack is built. ``attorney_review`` adds a "confirm with attorney" badge. Templates show
both with ``{% load rule_tags %}{% rule_badge resolved %}``. Board page:
``/settings/rules/``.

Keys renamed by the 2026-10 research (``rules.keys.RENAMED``) still resolve under their
old names, in packs, overrides and callers.

Writing a pack: build a ``rules.base.RulePack`` whose ``values`` use keys from
``rules.keys.KEYS`` and expose it as the ``"RulePack"`` provider in your plugin manifest.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

from .base import Basis, RulePack, RulePackError, RuleValue
from .keys import DELINQUENCY_STEPS, KEYS, NOTICE_KINDS, RENAMED, VOTE_KINDS, canonical

SETTINGS_KEY = "rules"

__all__ = ["Basis", "Resolved", "RulePack", "RulePackError", "RuleValue"]


@dataclasses.dataclass(frozen=True, kw_only=True)
class Resolved:
    key: str
    label: Any
    value: Any
    verified: bool
    basis: Basis
    citation: str
    note: str
    source: str  # "override" or the pack id that supplied the value
    pack_rule: RuleValue | None  # what the active pack says on ``as_of`` (even when unused)
    as_of: datetime.date | None = None
    quote: str = ""
    source_url: str = ""
    checked_on: datetime.date | None = None
    attorney_review: bool = False
    effective_from: datetime.date | None = None
    effective_to: datetime.date | None = None
    next_rule: RuleValue | None = None  # the active pack's next scheduled value
    clamped: bool = False  # an override above the statutory cap was lowered to it
    requested: Any = None  # the override value as set, when clamped
    cap_key: str = ""
    cap_citation: str = ""
    cap_source_url: str = ""

    @property
    def is_set(self) -> bool:
        return self.value is not None

    @property
    def next_value(self) -> Any:
        if self.next_rule is None or self.next_rule.value is None:
            return None
        return KEYS[self.key].coerce(self.next_rule.value)


def generic_pack() -> RulePack:
    from .packs.generic import pack

    return pack


def _canonical_pack(pack: RulePack) -> RulePack:
    old = [k for k in pack.values if k in RENAMED]
    if not old:
        return pack
    values = dict(pack.values)
    for key in old:
        moved = values.pop(key)
        values.setdefault(RENAMED[key], moved)
    return dataclasses.replace(pack, values=values)


def active_pack(association) -> RulePack:
    from homeoa import providers

    pack = providers.get_provider("RulePack", association)
    if not isinstance(pack, RulePack):
        raise TypeError(f"RulePack provider returned {type(pack).__name__}, not a RulePack.")
    pack = _canonical_pack(pack)
    unknown = set(pack.values) - set(KEYS)
    if unknown:
        raise ValueError(f"Rule pack {pack.id!r} sets unknown keys: {sorted(unknown)}")
    return pack


def _overrides(association) -> dict[str, Any]:
    if association is None:
        return {}
    data = (association.settings or {}).get(SETTINGS_KEY) or {}
    if not isinstance(data, dict):
        return {}
    out = {canonical(k): v for k, v in data.items() if k in RENAMED}
    out.update({k: v for k, v in data.items() if k not in RENAMED})
    return out


def _today(association) -> datetime.date:
    from homeoa.core.context import association_today

    return association_today(association)


def _rule_fields(rule: RuleValue) -> dict[str, Any]:
    return {
        "verified": rule.verified,
        "basis": rule.basis,
        "citation": rule.citation,
        "note": rule.note,
        "quote": rule.quote,
        "source_url": rule.source_url,
        "checked_on": rule.checked_on,
        "attorney_review": rule.attorney_review,
        "effective_from": rule.effective_from,
        "effective_to": rule.effective_to,
    }


def _cap(key: str, pack: RulePack, as_of: datetime.date) -> tuple[Any, RuleValue | None]:
    """The statutory maximum in force for ``key``'s override, from the active pack only."""
    cap_key = KEYS[key].cap_key
    if not cap_key:
        return None, None
    rule = pack.get(cap_key, as_of)
    if rule is None or rule.value is None:
        return None, None
    return KEYS[cap_key].coerce(rule.value), rule


def _resolve(key: str, pack: RulePack, overrides: dict[str, Any], as_of) -> Resolved:
    meta = KEYS[key]
    pack_rule = pack.get(key, as_of)
    common = {
        "key": key,
        "label": meta.label,
        "pack_rule": pack_rule,
        "as_of": as_of,
        "next_rule": pack.next_value(key, as_of),
    }
    if key in overrides:
        value = meta.coerce(overrides[key])
        extra: dict[str, Any] = {}
        cap, cap_rule = _cap(key, pack, as_of)
        if cap is not None and value is not None and value > cap:
            extra = {
                "clamped": True,
                "requested": value,
                "cap_key": meta.cap_key,
                "cap_citation": cap_rule.citation,
                "cap_source_url": cap_rule.source_url,
            }
            value = cap
        return Resolved(
            value=value,
            verified=False,
            basis=Basis.ASSOCIATION,
            citation="",
            note="",
            source="override",
            **extra,
            **common,
        )
    rule, source = pack_rule, pack.id
    if rule is None or rule.value is None:
        generic = generic_pack().get(key, as_of) or RuleValue(value=None, basis=Basis.BOARD)
        source = generic_pack().id
        if pack_rule is None:
            rule = generic
        else:  # keep the pack's citation and flags visible, but it is not verified
            rule = dataclasses.replace(
                pack_rule, value=generic.value, basis=generic.basis, verified=False
            )
    return Resolved(
        value=meta.coerce(rule.value) if rule.value is not None else None,
        source=source,
        **_rule_fields(rule),
        **common,
    )


def resolve(association, key: str, as_of: datetime.date | None = None) -> Resolved:
    """The rule in force on ``as_of`` (default: the association's today)."""
    key = canonical(key)
    if key not in KEYS:
        raise KeyError(f"Unknown rule key {key!r}.")
    as_of = as_of or _today(association)
    return _resolve(key, active_pack(association), _overrides(association), as_of)


def pack_rule(association, key: str, as_of: datetime.date | None = None) -> RuleValue | None:
    """What the active pack itself says for ``key`` on ``as_of`` (default: the
    association's today), ignoring the association's overrides and the generic pack.
    Legal limits read this, so a board cannot raise a cap by overriding it."""
    key = canonical(key)
    if key not in KEYS:
        raise KeyError(f"Unknown rule key {key!r}.")
    return active_pack(association).get(key, as_of or _today(association))


def value(association, key: str, as_of: datetime.date | None = None) -> Any:
    return resolve(association, key, as_of).value


def all_values(association, as_of: datetime.date | None = None) -> list[Resolved]:
    pack, overrides = active_pack(association), _overrides(association)
    as_of = as_of or _today(association)
    return [_resolve(key, pack, overrides, as_of) for key in KEYS]


_CLEAR = object()


def _write_override(association, key: str, new: Any, reason: str, clamped_to: Any = None):
    from homeoa import audit

    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to change a rule.")
    settings = dict(association.settings or {})
    overrides = dict(settings.get(SETTINGS_KEY) or {})
    for legacy, current in RENAMED.items():  # saved under an old name: move it
        if current == key and legacy in overrides:
            overrides.setdefault(key, overrides.pop(legacy))
    old = overrides.get(key)
    if new is _CLEAR:
        if key not in overrides:
            return
        overrides.pop(key)
        new = None
    else:
        overrides[key] = new
    settings[SETTINGS_KEY] = overrides
    association.settings = settings
    association.save(update_fields=["settings"])
    changes = {"key": key, "old": old, "new": new, "reason": reason}
    if clamped_to is not None:
        changes["clamped_to"] = clamped_to
    audit.record("rules.override", association, changes=changes, message=reason)


def set_override(association, key: str, raw: Any, *, reason: str) -> Any:
    """Set an association override. ``reason`` is required; audited as ``rules.override``.

    A value above the statutory cap in force (``RuleKey.cap_key``) is saved as asked but
    resolves to the cap (``Resolved.clamped``); the audit event records ``clamped_to``.
    Raises ``KeyError`` (unknown key) or ``ValueError`` (bad value, missing reason).
    """
    key = canonical(key)
    if key not in KEYS:
        raise KeyError(f"Unknown rule key {key!r}.")
    meta = KEYS[key]
    coerced = meta.coerce(raw)
    clamped_to = None
    if coerced is not None:
        cap, _rule = _cap(key, active_pack(association), _today(association))
        if cap is not None and coerced > cap:
            clamped_to = meta.to_json(cap)
    _write_override(association, key, meta.to_json(coerced), reason, clamped_to)
    return coerced


def clear_override(association, key: str, *, reason: str) -> None:
    """Remove an override (back to the pack value). Same reason and audit rules."""
    key = canonical(key)
    if key not in KEYS:
        raise KeyError(f"Unknown rule key {key!r}.")
    _write_override(association, key, _CLEAR, reason)


# Typed accessors


def payment_allocation_order(association) -> tuple[str, ...]:
    return value(association, "payments.allocation_order")


def late_fee_cap(association):
    """``Decimal`` cap, or ``None`` for no cap."""
    return value(association, "late_fees.cap")


def notice_window(association, kind: str) -> tuple[int | None, int | None]:
    """``(min_days, max_days)`` before the event that notice must go out."""
    if kind not in NOTICE_KINDS:
        raise KeyError(f"Unknown notice kind {kind!r}.")
    return (
        value(association, f"notices.{kind}.min_days"),
        value(association, f"notices.{kind}.max_days"),
    )


def fine_due_process_steps(association) -> tuple[str, ...] | None:
    return value(association, "fines.due_process_steps")


def vote_threshold(association, kind: str):
    if kind not in VOTE_KINDS:
        raise KeyError(f"Unknown vote kind {kind!r}.")
    return value(association, f"votes.{kind}.threshold_percent")


def delinquency_offsets(association) -> dict[str, Resolved]:
    """``{step: Resolved}`` for each delinquency ladder step, in ladder order.

    ``Resolved.value`` is days after the charge's due date, or ``None`` (step off).
    """
    return {step: resolve(association, f"delinquency.{step}_days") for step in DELINQUENCY_STEPS}
