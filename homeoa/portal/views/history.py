"""Payment history (POR-03): the owner's ledger and receipts, own ownership periods only."""

from __future__ import annotations

from homeoa.access import permissions as perms
from homeoa.access.decorators import resident_view
from homeoa.access.services import today_for

from .base import _render
from .mine import my_periods


@resident_view(perm=perms.BALANCE_OWN)
def payments(request):
    from homeoa.billing.owner_ledger import owner_ledger
    from homeoa.payments.models import Receipt

    today = today_for(request.association)
    sections = []
    for period in my_periods(request):
        rows = owner_ledger(period.unit, ownership_period_id=period.pk, as_of=today)
        receipts = Receipt.objects.filter(ownership_period=period).order_by("-received_date", "-pk")
        sections.append(
            {
                "period": period,
                "unit": period.unit,
                "rows": list(reversed(rows)),
                "balance": rows[-1].balance if rows else None,
                "receipts": list(receipts),
            }
        )
    return _render(request, "pay", "portal/payments.html", sections=sections)
