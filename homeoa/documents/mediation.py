"""Itemized unit ledger for foreclosure mediation (FIN-17, RCW 61.24.163(7)(a)).

The export covers the longer of the 12 months before the export date or the time since
the account became past due (the due date of the oldest charge still open and past due),
never before the current ownership period starts. It lists every charge and payment
with date, type and amount, a balance-forward line, and the total balance on the export
date, which equals the unit subledger for the ownership period.
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Min
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.billing.owner_ledger import ZERO, owner_ledger
from homeoa.core.context import association_today
from homeoa.providers import get_provider

from .models import LedgerExport

CSV_COLUMNS = ["date", "type", "description", "charge", "payment", "balance"]


class NoOwnershipPeriod(LookupError):
    pass


@dataclass
class Item:
    date: dt.date
    type: str
    description: str
    charge: Decimal
    payment: Decimal
    balance: Decimal


@dataclass
class Export:
    unit: object
    period: object
    as_of: dt.date
    start: dt.date
    past_due_since: dt.date | None
    opening: Decimal
    items: list[Item]
    balance: Decimal


def months_back(day: dt.date, months: int = 12) -> dt.date:
    y, m = divmod(day.year * 12 + day.month - 1 - months, 12)
    m += 1
    return day.replace(year=y, month=m, day=min(day.day, calendar.monthrange(y, m)[1]))


def current_period(unit, as_of):
    from homeoa.registry.models import OwnershipPeriod

    for p in OwnershipPeriod.all_associations.filter(unit_id=unit.pk).order_by("-start"):
        if p.covers(as_of):
            return p
    raise NoOwnershipPeriod(unit.number)


def past_due_since(unit, period, as_of) -> tuple[dt.date | None, dt.date | None]:
    """``(past_due_since, charged_on)`` of the oldest charge still open and past due."""
    from homeoa.billing.models import Charge

    agg = Charge.all_associations.filter(
        unit_id=unit.pk,
        ownership_period_id=period.pk,
        open_balance__gt=0,
        due_date__lt=as_of,
        void_entry__isnull=True,
    ).aggregate(d=Min("due_date"), c=Min("charge_date"))
    return agg["d"], agg["c"]


def date_range(unit, period, as_of) -> tuple[dt.date, dt.date | None]:
    """``(start, past_due_since)``: 12 months back, or earlier when the account has been
    past due longer (then from the oldest open charge's date, so it is itemized)."""
    since, charged = past_due_since(unit, period, as_of)
    start = months_back(as_of)
    if charged is not None and charged < start:
        start = charged
    return max(start, period.start), since


def _types(entry_ids) -> dict[int, str]:
    from homeoa.billing.models import Adjustment, Charge, ChargeKind
    from homeoa.payments.models import Receipt, Refund

    labels = {
        ChargeKind.ASSESSMENT: _("Dues"),
        ChargeKind.SPECIAL: _("Special assessment"),
        ChargeKind.LATE_FEE: _("Late fee"),
        ChargeKind.INTEREST: _("Interest"),
        ChargeKind.OTHER: _("Other charge"),
        ChargeKind.FINE: _("Fine"),
    }
    out: dict[int, str] = {}
    for kind, e in Charge.all_associations.filter(entry_id__in=entry_ids).values_list(
        "kind", "entry_id"
    ):
        out[e] = str(labels.get(kind, _("Other charge")))
    for e in Charge.all_associations.filter(void_entry_id__in=entry_ids).values_list(
        "void_entry_id", flat=True
    ):
        out[e] = _("Charge voided")
    for adj in Adjustment.all_associations.filter(entry_id__in=entry_ids):
        out.setdefault(adj.entry_id, str(adj.get_kind_display()))
    for e in Receipt.all_associations.filter(entry_id__in=entry_ids).values_list(
        "entry_id", flat=True
    ):
        out[e] = _("Payment")
    for e in Receipt.all_associations.filter(void_entry_id__in=entry_ids).values_list(
        "void_entry_id", flat=True
    ):
        out[e] = _("Payment reversed")
    for e in Refund.all_associations.filter(entry_id__in=entry_ids).values_list(
        "entry_id", flat=True
    ):
        out[e] = _("Refund")
    return out


def build(unit, as_of: dt.date | None = None) -> Export:
    as_of = as_of or association_today(unit.association)
    period = current_period(unit, as_of)
    start, since = date_range(unit, period, as_of)
    rows = owner_ledger(unit, ownership_period_id=period.pk, as_of=as_of)
    opening = ZERO
    kept = []
    for r in rows:
        if r.date < start:
            opening = r.balance
        else:
            kept.append(r)
    types = _types([r.entry_id for r in kept])
    items = [
        Item(
            date=r.date,
            type=types.get(r.entry_id) or (_("Payment") if r.payment else _("Other charge")),
            description=r.description,
            charge=r.charge,
            payment=r.payment,
            balance=r.balance,
        )
        for r in kept
    ]
    balance = rows[-1].balance if rows else ZERO
    return Export(unit, period, as_of, start, since, opening, items, balance)


def to_csv(exp: Export) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    w.writerow([exp.start.isoformat(), _("Balance forward"), "", "", "", f"{exp.opening:.2f}"])
    for i in exp.items:
        w.writerow(
            [
                i.date.isoformat(),
                i.type,
                i.description,
                f"{i.charge:.2f}" if i.charge else "",
                f"{i.payment:.2f}" if i.payment else "",
                f"{i.balance:.2f}",
            ]
        )
    w.writerow([exp.as_of.isoformat(), _("Total balance"), "", "", "", f"{exp.balance:.2f}"])
    return buf.getvalue().encode("utf-8")


def _fmt(d: dt.date) -> str:
    return date_format(d, "M j, Y")


def to_pdf(exp: Export) -> bytes:
    from homeoa.letters import pdf
    from homeoa.letters.letterhead import letterhead
    from homeoa.letters.merge import money

    names = ", ".join(p.full_name for p in exp.period.owners.all())
    title = _("Unit %(unit)s itemized ledger") % {"unit": exp.unit.number}
    header = [_("Date"), _("Type"), _("Description"), _("Charge"), _("Payment"), _("Balance")]
    rows = [header, [_fmt(exp.start), _("Balance forward"), "", "", "", money(exp.opening)]]
    for i in exp.items:
        rows.append(
            [
                _fmt(i.date),
                i.type,
                i.description[:50],
                money(i.charge) if i.charge else "",
                money(i.payment) if i.payment else "",
                money(i.balance),
            ]
        )
    rows.append([_fmt(exp.as_of), _("Total balance"), "", "", "", money(exp.balance)])
    body = _(
        "Itemized ledger of unit %(unit)s (owner of record: %(owners)s) from %(start)s "
        "to %(end)s: every charge and payment with date and amount, and the total balance "
        "as of %(end)s."
    ) % {
        "unit": exp.unit.number,
        "owners": names or "-",
        "start": _fmt(exp.start),
        "end": _fmt(exp.as_of),
    }
    if exp.past_due_since:
        body += "\n\n" + _("Account past due since %(date)s.") % {"date": _fmt(exp.past_due_since)}
    return pdf.build_pdf(
        letterhead=letterhead(exp.unit.association),
        address_lines=[],
        date_text=_fmt(exp.as_of),
        subject=title,
        body=body,
        title=title,
        tables=[{"rows": rows, "num_cols": [3, 4, 5], "bold_last": True}],
    )


@transaction.atomic
def export(unit, *, as_of: dt.date | None = None, user=None) -> LedgerExport:
    """Build, store (PDF + CSV) and audit one export; it then shows in the unit file."""
    exp = build(unit, as_of)
    assoc = unit.association
    storage = get_provider("FileStorage", assoc)
    stem = f"ledger-{unit.number}-{exp.as_of.isoformat()}"
    p = storage.put(
        assoc.pk, stem + ".pdf", to_pdf(exp), content_type="application/pdf", folder="ledgers"
    )
    c = storage.put(assoc.pk, stem + ".csv", to_csv(exp), content_type="text/csv", folder="ledgers")
    rec = LedgerExport.objects.create(
        association=assoc,
        unit=unit,
        ownership_period=exp.period,
        as_of=exp.as_of,
        start=exp.start,
        past_due_since=exp.past_due_since,
        balance=exp.balance,
        pdf_key=p.key,
        csv_key=c.key,
        pdf_sha256=p.sha256,
        csv_sha256=c.sha256,
        generated_by=user if user is not None and user.is_authenticated else None,
    )
    audit.record(
        "unit.ledger_export",
        rec,
        changes={
            "unit": unit.number,
            "start": exp.start.isoformat(),
            "as_of": exp.as_of.isoformat(),
            "balance": str(exp.balance),
            "pdf_sha256": p.sha256,
            "csv_sha256": c.sha256,
        },
    )
    return rec
