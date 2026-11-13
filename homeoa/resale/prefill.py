"""Draft answers for certificate items from what HomeOA already knows.

Each source returns plain text for one unit on one date, or ``""`` when there is nothing
to say; the board reviews and edits every answer before issuing. Money comes from the
ledger and billing services only.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.utils.translation import gettext as _

from homeoa.ledger.templatetags.ledger_tags import money


def unit_charges(unit, on: dt.date) -> str:
    from homeoa.billing.latefee_models import SpecialAssessment
    from homeoa.billing.models import AssessmentSchedule
    from homeoa.billing.services import month_start, unit_dues
    from homeoa.ledger.services import unit_balance
    from homeoa.registry.services import ownership_period_on

    lines = []
    period = month_start(on)
    for schedule in AssessmentSchedule.all_associations.filter(
        association=unit.association, active=True
    ):
        if schedule.covers(period):
            amount = unit_dues(schedule).get(unit.pk)
            if amount is not None:
                lines.append(_("Monthly assessment: %(amount)s.") % {"amount": money(amount)})
    op = ownership_period_on(unit, on)
    balance = unit_balance(
        unit.pk, as_of=on, ownership_period_id=op.pk if op else None, association=unit.association
    )
    if balance > 0:
        lines.append(
            _("Unpaid and due as of %(date)s: %(amount)s.")
            % {"date": on.isoformat(), "amount": money(balance)}
        )
    else:
        lines.append(_("Nothing unpaid as of %(date)s.") % {"date": on.isoformat()})
    specials = SpecialAssessment.all_associations.filter(association=unit.association)
    for sa in specials:
        remaining = sa.installments.filter(posted_at__isnull=True).order_by("due_date")
        if not remaining.exists():
            continue
        lines.append(
            _(
                "Special assessment levied: %(name)s, %(n)s installment(s) still to bill, "
                "last due %(last)s."
            )
            % {
                "name": sa.name,
                "n": remaining.count(),
                "last": remaining.last().due_date.isoformat(),
            }
        )
    return "\n".join(lines)


def reserves(unit, on: dt.date) -> str:
    from homeoa.dashboard.cards import cash_by_fund

    rows = [(fund, bal) for fund, bal in cash_by_fund(unit.association) if fund.is_reserve]
    if not rows:
        return ""
    return "\n".join(
        _("%(fund)s: %(amount)s in the bank.") % {"fund": fund.name, "amount": money(bal)}
        for fund, bal in rows
    )


def budget(unit, on: dt.date) -> str:
    from homeoa.budget.services import adopted_for, totals

    b = adopted_for(unit.association, on.year)
    if b is None:
        return ""
    t = totals(b)
    return _(
        "%(name)s (%(year)s): income %(income)s, expenses %(expense)s. Copy attached on request."
    ) % {
        "name": b.name,
        "year": b.year,
        "income": money(t["income"]),
        "expense": money(t["expense"]),
    }


def insurance(unit, on: dt.date) -> str:
    from homeoa.insurance.services import current

    p = current(unit.association, on)
    if p is None:
        return ""
    parts = [
        _("Master policy with %(carrier)s, %(start)s to %(end)s.")
        % {"carrier": p.carrier, "start": p.start_date.isoformat(), "end": p.end_date.isoformat()}
    ]
    if p.property_limit:
        parts.append(_("Property limit %(amount)s.") % {"amount": money(p.property_limit)})
    if p.liability_limit:
        parts.append(_("Liability limit %(amount)s.") % {"amount": money(p.liability_limit)})
    if p.deductible:
        parts.append(_("Deductible %(amount)s.") % {"amount": money(p.deductible)})
    agent = ", ".join(x for x in (p.agent_name, p.agent_phone, p.agent_email) if x)
    if agent:
        parts.append(_("Agent: %(agent)s.") % {"agent": agent})
    return " ".join(parts)


def governing_documents(unit, on: dt.date) -> str:
    from homeoa.documents.models import Category, Document, Visibility

    docs = Document.all_associations.filter(
        association=unit.association,
        category=Category.GOVERNING,
        current_version__isnull=False,
        status="",
    ).exclude(visibility=Visibility.BOARD)
    if not docs:
        return ""
    return _("Provided: %(titles)s.") % {
        "titles": "; ".join(d.title for d in docs.order_by("title"))
    }


def _open_charges(unit, on: dt.date, assessments: bool):
    from homeoa.billing.models import Charge, ChargeKind

    kinds = (ChargeKind.ASSESSMENT, ChargeKind.SPECIAL)
    charges = Charge.all_associations.filter(
        association=unit.association, unit=unit, charge_date__lte=on
    )
    charges = charges.filter(kind__in=kinds) if assessments else charges.exclude(kind__in=kinds)
    return [c for c in charges.order_by("due_date", "pk") if c.open_balance > 0]


def unpaid_assessments(unit, on: dt.date) -> str:
    rows = _open_charges(unit, on, assessments=True)
    if not rows:
        return _("None: no assessment is unpaid as of %(date)s.") % {"date": on.isoformat()}
    total = sum((c.open_balance for c in rows), Decimal("0.00"))
    lines = [
        _("%(amount)s unpaid as of %(date)s:") % {"amount": money(total), "date": on.isoformat()}
    ]
    lines += [
        _("%(what)s, due %(due)s: %(amount)s")
        % {"what": c.description, "due": c.due_date.isoformat(), "amount": money(c.open_balance)}
        for c in rows
    ]
    return "\n".join(lines)


def other_unpaid(unit, on: dt.date) -> str:
    rows = _open_charges(unit, on, assessments=False)
    if not rows:
        return _("None.")
    return "\n".join(
        _("%(what)s, due %(due)s: %(amount)s")
        % {"what": c.description, "due": c.due_date.isoformat(), "amount": money(c.open_balance)}
        for c in rows
    )


# Sources core answers itself. Optional modules add theirs (for example ``violations``)
# to the RESALE_CERTIFICATE_ANSWERS slot as ``(source key, function)`` or
# ``(source key, function, off text)``; the off text is the draft while the module is off.
SOURCES = {
    "unpaid_assessments": unpaid_assessments,
    "other_unpaid": other_unpaid,
    "unit_charges": unit_charges,
    "reserves": reserves,
    "budget": budget,
    "insurance": insurance,
    "governing_documents": governing_documents,
}


def off_note(source: str) -> str:
    """The draft a module-owned ``source`` gets while its module is off, or ``""`` for a
    source core answers itself. The board must replace it before issuing."""
    from homeoa.plugins import slots

    for c in slots.contributions(slots.RESALE_CERTIFICATE_ANSWERS):
        key, _source, *off = c.value
        if key == source:
            return (
                str(off[0]) if off else _("Not tracked in HomeOA. The board must answer this item.")
            )
    return ""


def _answer(item, unit, on: dt.date) -> str:
    if item.source == "statutory":  # mandated wording, printed verbatim
        from homeoa.letters import statutory

        return statutory.get(item.text_id).text.strip()
    if not item.source:
        return ""
    if item.source in SOURCES:
        return SOURCES[item.source](unit, on)
    from homeoa.plugins import slots

    # Every contribution, so a source whose module is off is known rather than missing.
    for c in slots.contributions(slots.RESALE_CERTIFICATE_ANSWERS):
        key, source, *_off = c.value
        if key == item.source:
            if not slots.module_on(c.module, unit.association):
                # The module is off, so HomeOA has nothing to go on: say so, and leave the
                # answer to the board.
                return off_note(item.source)
            return source(unit, on)
    raise KeyError(item.source)


def draft(form, unit, on: dt.date) -> dict[str, str]:
    """``{item key: draft answer}`` for every item of ``form``; board-only items are ``""``."""
    return {item.key: _answer(item, unit, on) for item in form.items}
