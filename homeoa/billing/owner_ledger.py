"""Owner ledger for one unit (FIN-04 view): charges, payments and a running balance.

Built from the unit-subledger journal lines (receivable 1100, prepaid 2100), one row per
journal entry. Entries that net to zero for the unit (e.g. applying a prepaid credit to a
charge) move money between subledger accounts without changing what is owed, so they are
left out. The final running balance always equals ``ledger.services.unit_balance``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum

ZERO = Decimal("0.00")


@dataclass
class LedgerRow:
    date: dt.date
    description: str
    charge: Decimal
    payment: Decimal
    balance: Decimal
    entry_id: int


def owner_ledger(unit, *, ownership_period_id: int | None = None, as_of=None) -> list[LedgerRow]:
    """Rows oldest first. With ``ownership_period_id`` only that owner's lines count."""
    from homeoa.ledger.models import JournalLine

    qs = JournalLine.all_associations.filter(
        association_id=unit.association_id, unit_id=unit.pk, account__in_unit_subledger=True
    )
    if ownership_period_id is not None:
        qs = qs.filter(ownership_period_id=ownership_period_id)
    if as_of is not None:
        qs = qs.filter(entry__date__lte=as_of)
    grouped = (
        qs.values("entry_id", "entry__date", "entry__memo")
        .annotate(d=Sum("debit"), c=Sum("credit"))
        .order_by("entry__date", "entry_id")
    )
    rows: list[LedgerRow] = []
    balance = ZERO
    for g in grouped:
        net = (g["d"] or ZERO) - (g["c"] or ZERO)
        if not net:
            continue
        balance += net
        rows.append(
            LedgerRow(
                date=g["entry__date"],
                description=g["entry__memo"],
                charge=net if net > 0 else ZERO,
                payment=-net if net < 0 else ZERO,
                balance=balance,
                entry_id=g["entry_id"],
            )
        )
    return rows
