"""Home to-do "Close <Month YYYY>" (FIN-15).

At most one item: the oldest month that has ended but is not closed. That is the month after
``locked_through``, or the month of the earliest ledger entry when nothing is locked yet.
Shown only to ``finance.manage``; it clears once the month is closed.
"""

from __future__ import annotations

import datetime as dt

from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.core.context import association_today
from homeoa.ledger import services as ledger
from homeoa.ledger.models import JournalEntry

from . import services


def next_open_month(association) -> dt.date | None:
    """First day of the oldest unclosed month, or ``None`` with an empty ledger."""
    locked = ledger.get_locked_through(association)
    if locked is not None:
        first = locked.replace(day=1)
        return first.replace(year=first.year + (first.month == 12), month=first.month % 12 + 1)
    earliest = (
        JournalEntry.all_associations.filter(association=association)
        .order_by("date")
        .values_list("date", flat=True)
        .first()
    )
    return earliest.replace(day=1) if earliest else None


def todo_items(association) -> list[dict]:
    first = next_open_month(association)
    if first is None:
        return []
    start, end = services.month_bounds(first.year, first.month)
    today = association_today(association)
    if end >= today:
        return []
    checks = services.checklist(association, start, end)
    failing = [c for c in checks if not c.ok]
    if failing:
        sub = "; ".join(f"{c.label}: {c.detail}" if c.detail else str(c.label) for c in failing)
    else:
        sub = _("Ready: every check passes")
    month = date_format(start, "F Y")
    return [
        {
            "id": f"close-{start:%Y-%m}",
            "kind": "money",
            "icon": "clock",
            "title": _("Close %(month)s") % {"month": month},
            "sub": sub,
            "due": end + dt.timedelta(days=1),
            "url": f"{reverse('closing:index')}?month={start:%Y-%m}",
            "action": _("Review"),
            "perm": perms.FINANCE_MANAGE,
        }
    ]
