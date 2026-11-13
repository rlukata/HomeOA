"""Merge-field registry. Apps contribute fields; templates may only use registered ones.

Register a field from your app's ``AppConfig.ready()``::

    from homeoa.letters import merge
    merge.register("violation.title", _("Violation title"), lambda ctx: ..., sample="Noise")

A resolver gets a ``LetterContext`` and returns a string (or something ``str()`` turns
into the right text: format money yourself with ``merge.money``). Field keys are dotted
``<group>.<name>``; templates write them as ``{{ unit.number }}``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _


@dataclasses.dataclass
class LetterContext:
    association: Any
    as_of: dt.date
    unit: Any = None
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)
    _cache: dict[str, Any] = dataclasses.field(default_factory=dict, repr=False)

    def cached(self, name: str, fn: Callable[[], Any]) -> Any:
        if name not in self._cache:
            self._cache[name] = fn()
        return self._cache[name]


@dataclasses.dataclass(frozen=True)
class MergeField:
    key: str
    label: Any
    resolve: Callable[[LetterContext], Any]
    sample: str = ""
    #: The optional module the field belongs to; pickers leave it out while it is off.
    module: str | None = None


_FIELDS: dict[str, MergeField] = {}


def register(
    key: str,
    label,
    resolve: Callable[[LetterContext], Any],
    *,
    sample: str = "",
    module: str | None = None,
):
    parts = key.split(".")
    if len(parts) != 2 or not all(p.isidentifier() for p in parts):
        raise ValueError(f"Merge field keys look like 'group.name', got {key!r}.")
    _FIELDS[key] = MergeField(key, label, resolve, sample, module)


def unregister(key: str) -> None:
    _FIELDS.pop(key, None)


def fields(association=None) -> dict[str, MergeField]:
    """Every merge field by key; given an association, without those of modules that are
    off for it."""
    from homeoa.plugins.slots import module_on

    return {k: f for k, f in sorted(_FIELDS.items()) if module_on(f.module, association)}


def is_known(key: str) -> bool:
    return key in _FIELDS


def money(value: Decimal) -> str:
    value = Decimal(value).quantize(Decimal("0.01"))
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def resolve_all(ctx: LetterContext, keys=None) -> dict[str, dict[str, Any]]:
    """Nested ``{"unit": {"number": "A"}}`` for the given keys (default: all)."""
    out: dict[str, dict[str, Any]] = {}
    for key in keys if keys is not None else _FIELDS:
        field = _FIELDS[key]
        group, name = key.split(".")
        value = field.resolve(ctx)
        out.setdefault(group, {})[name] = "" if value is None else value
    return out


def sample_values() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, field in _FIELDS.items():
        group, name = key.split(".")
        out.setdefault(group, {})[name] = field.sample
    return out


# --- built-in fields (association, letter, unit, owner, balance) ------------------------


def _letterhead(ctx):
    from .letterhead import letterhead

    return ctx.cached("letterhead", lambda: letterhead(ctx.association))


def _pay_url(ctx) -> str:
    from django.conf import settings
    from django.urls import reverse

    from homeoa.core import residents_enabled

    if ctx.association is None or not residents_enabled(ctx.association):
        return ""
    return (getattr(settings, "HOMEOA_BASE_URL", "") or "") + reverse("portal:pay")


def _owner_info(ctx):
    def load():
        from homeoa.registry.services import ownership_period_on

        if ctx.unit is None:
            return None, []
        period = ownership_period_on(ctx.unit, ctx.as_of)
        if period is None:
            return None, []
        shares = list(period.shares.select_related("person").order_by("-is_primary", "pk"))
        return period, [s.person for s in shares]

    return ctx.cached("owners", load)


def _primary_owner(ctx):
    _period, people = _owner_info(ctx)
    return people[0] if people else None


def owner_names(ctx) -> str:
    _period, people = _owner_info(ctx)
    return " & ".join(p.full_name for p in people)


def mailing_address_lines(ctx) -> list[str]:
    """Primary owner's mailing address, else the unit's own address."""
    person = _primary_owner(ctx)
    if person is not None and person.address_lines:
        return person.address_lines
    return list(ctx.unit.address_lines) if ctx.unit is not None else []


def _balance(ctx):
    def load():
        from homeoa.ledger.services import unit_balance

        if ctx.unit is None:
            return Decimal("0")
        return unit_balance(ctx.unit.pk, as_of=ctx.as_of, association=ctx.association)

    return ctx.cached("balance", load)


def _register_builtins():
    register(
        "association.name",
        _("Association name"),
        lambda c: c.association.name,
        sample="Cedar Row Condominium",
    )
    register(
        "association.short_name",
        _("Association short name"),
        lambda c: c.association.short_name,
        sample="CR",
    )
    register(
        "association.mailing_address",
        _("Association mailing address (one line)"),
        lambda c: ", ".join(_letterhead(c)["address_lines"]),
        sample="PO Box 100, Tukwila, WA 98188",
    )
    register(
        "association.email",
        _("Association email"),
        lambda c: _letterhead(c)["email"],
        sample="board@example.org",
    )
    register(
        "association.phone",
        _("Association phone"),
        lambda c: _letterhead(c)["phone"],
        sample="(206) 555-0100",
    )
    register(
        "association.pay_url",
        _("Owner portal Pay page (empty while the portal is off for residents)"),
        _pay_url,
        sample="https://hoa.example.org/portal/pay/",
    )
    register(
        "letter.date",
        _("Letter date"),
        lambda c: date_format(c.as_of, "N j, Y"),
        sample="Oct. 7, 2026",
    )
    register("unit.number", _("Unit number"), lambda c: c.unit.number if c.unit else "", sample="A")
    register(
        "unit.address",
        _("Unit address (one line)"),
        lambda c: ", ".join(c.unit.address_lines) if c.unit else "",
        sample="100 Cedar Row, Unit A, Tukwila, WA 98188",
    )
    register("owner.name", _("Owner name(s)"), owner_names, sample="Jordan & Sam Rivera")
    register(
        "owner.first_name",
        _("Primary owner's preferred or first name"),
        lambda c: (p.preferred_name or p.full_name.split()[0]) if (p := _primary_owner(c)) else "",
        sample="Jordan",
    )
    register(
        "owner.mailing_address",
        _("Owner mailing address (one line)"),
        lambda c: ", ".join(mailing_address_lines(c)),
        sample="100 Cedar Row, Unit A, Tukwila, WA 98188",
    )
    register(
        "balance.total",
        _("Balance owed as of the letter date"),
        lambda c: money(_balance(c)),
        sample="$1,250.00",
    )


_register_builtins()
