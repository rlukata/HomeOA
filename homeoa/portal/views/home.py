"""Resident Home (POR-01, REG-06): owner dashboard and tenant limited view.

``home`` resolves the signed-in resident's unit (``?unit=<number>`` switches between
several) and calls ``render_home``. ``render_home`` takes an explicit ``unit`` and a
``read_only`` flag so the board's preview-as-unit page can reuse it.

Money comes only from service functions: ``ledger.services.unit_balance``,
``payments.services.open_charges`` and ``billing.owner_ledger.owner_ledger``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from homeoa.access.decorators import resident_view
from homeoa.access.services import today_for, units_for

from .base import _render

ZERO = Decimal("0.00")
RECENT_ROWS = 5


@dataclass
class MoneySummary:
    balance: Decimal
    label: str
    due_date: dt.date | None = None
    overdue: bool = False
    recent: list = field(default_factory=list)


def is_owner_of(user, unit, on: dt.date) -> bool:
    from homeoa.registry.services import current_owners

    return any(p.user_id == user.pk for p in current_owners(unit, on))


def money_summary(unit, today: dt.date) -> MoneySummary:
    """Balance, next due line and recent activity for the unit's current owner."""
    from homeoa.billing.owner_ledger import owner_ledger
    from homeoa.ledger.services import unit_balance
    from homeoa.payments.services import open_charges
    from homeoa.registry.services import ownership_period_on

    period = ownership_period_on(unit, today)
    period_id = period.pk if period else None
    balance = unit_balance(
        unit.pk,
        as_of=today,
        ownership_period_id=period_id,
        association=unit.association,
    )
    charges = [c for c in open_charges(unit, period) if c.charge_date <= today]
    due_date = min((c.due_date for c in charges), default=None) if balance > 0 else None
    if balance > 0 and len(charges) == 1:
        label = _("Unit %(number)s · %(what)s") % {
            "number": unit.number,
            "what": charges[0].description,
        }
    else:
        label = _("Unit %(number)s balance") % {"number": unit.number}
    rows = owner_ledger(unit, ownership_period_id=period_id, as_of=today)
    return MoneySummary(
        balance=balance,
        label=label,
        due_date=due_date,
        overdue=bool(due_date and due_date < today),
        recent=list(reversed(rows[-RECENT_ROWS:])),
    )


def render_home(
    request, *, unit, read_only: bool, show_money: bool | None = None, units=(), extra=None
):
    """Render Home for ``unit``. ``show_money`` defaults to "the user owns this unit".

    ``extra`` overrides template context (the preview passes its own tabs and banner).
    """
    from homeoa.registry.services import current_owners

    today = today_for(request.association)
    extra = {**(extra or {}), "unit": unit, "read_only": read_only, "switch_units": list(units)}
    if unit is not None:
        if show_money is None:
            show_money = is_owner_of(request.user, unit, today)
        extra["show_money"] = show_money
        if show_money:
            extra["money"] = money_summary(unit, today)
            if not read_only:
                extra["quick_links"] = [
                    (_("Pay"), reverse("portal:pay"), "money"),
                    (_("Payment history"), reverse("portal:payments"), "check"),
                    (_("Statements"), reverse("portal:statements"), "doc"),
                    (_("Documents"), reverse("portal:docs"), "doc"),
                ]
        else:
            extra["owner_names"] = ", ".join(p.full_name for p in current_owners(unit, today))
    if not read_only:
        from .mine import my_person
        from .profile import consent_context, needs_consent_prompt

        person = my_person(request)
        if extra.get("show_money") and needs_consent_prompt(person):
            extra.update(consent_prompt=True, person=person, **consent_context())
        if extra.get("show_money"):
            from homeoa.documents import welcome

            extra["welcome_prompt"] = not welcome.has_seen(request.user, request.association)
    return _render(request, "home", "portal/home.html", **extra)


@resident_view
def home(request):
    units = units_for(request.user, request.association)
    wanted = request.GET.get("unit")
    units = sorted(units, key=lambda u: u.number)
    unit = next((u for u in units if u.number == wanted), None) or (units[0] if units else None)
    return render_home(request, unit=unit, read_only=False, units=units)
