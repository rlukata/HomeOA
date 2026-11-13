"""1099 tracking (FIN-19): cash paid per 1099-eligible vendor in a calendar year.

Cash basis: a bill counts in the year it was paid (``Bill.paid_on``), for its full
total; voided bills never count. Bills paid by card or a payment app are left out: the
processor reports those on 1099-K (``Bill.REPORTED_BY_PROCESSOR``). The same table feeds
the Vendors > 1099 tab and the CPA package, so both files are identical. No IRS threshold
lives here: if one is shown it comes from ``settings.HOMEOA_1099_THRESHOLD`` (unverified,
see the legal backlog).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils.translation import gettext as _

from homeoa.reports.export import MONEY, Column, TableSpec

ZERO = Decimal("0.00")


@dataclass
class Row:
    vendor: object
    paid: Decimal
    by_processor: Decimal = ZERO

    @property
    def w9_on_file(self) -> bool:
        return bool(self.vendor.w9_file_key or self.vendor.w9_received)


@dataclass
class Totals:
    year: int
    rows: list[Row]

    @property
    def total(self) -> Decimal:
        return sum((r.paid for r in self.rows), ZERO)

    @property
    def by_processor(self) -> Decimal:
        return sum((r.by_processor for r in self.rows), ZERO)

    @property
    def missing_w9(self) -> list[Row]:
        return [r for r in self.rows if not r.w9_on_file and r.paid]


def totals(association, year: int) -> Totals:
    """Every 1099-eligible vendor (paid or not) with what was paid in calendar ``year``."""
    from homeoa.ap.models import Bill, BillLine
    from homeoa.vendors.models import Vendor

    lines = BillLine.all_associations.filter(
        association=association,
        bill__status=Bill.Status.PAID,
        bill__paid_on__gte=dt.date(year, 1, 1),
        bill__paid_on__lte=dt.date(year, 12, 31),
        bill__vendor__is_1099_eligible=True,
    )
    processor = Bill.REPORTED_BY_PROCESSOR
    paid = dict(
        lines.exclude(bill__paid_by__in=processor)
        .values_list("bill__vendor_id")
        .annotate(s=Sum("amount"))
    )
    by_processor = dict(
        lines.filter(bill__paid_by__in=processor)
        .values_list("bill__vendor_id")
        .annotate(s=Sum("amount"))
    )
    vendors = Vendor.all_associations.filter(association=association, is_1099_eligible=True)
    rows = [
        Row(v, paid.get(v.pk) or ZERO, by_processor.get(v.pk) or ZERO)
        for v in vendors.order_by("name", "pk")
    ]
    return Totals(year, rows)


def threshold() -> Decimal | None:
    """Reporting threshold to flag, from settings only (unverified; never a code default)."""
    value = getattr(settings, "HOMEOA_1099_THRESHOLD", None)
    return Decimal(str(value)) if value not in (None, "") else None


def table(t: Totals) -> TableSpec:
    return TableSpec(
        title=_("1099 vendor totals"),
        filename="1099-vendors",
        subtitle=_("Calendar year %(y)s · cash paid by payment date") % {"y": t.year},
        columns=[
            Column(_("Vendor")),
            Column(_("Legal name")),
            Column(_("TIN")),
            Column(_("W-9 on file")),
            Column(_("Paid"), MONEY),
            Column(_("Paid by card or app (1099-K, left out)"), MONEY),
        ],
        rows=[
            [
                r.vendor.name,
                r.vendor.legal_name,
                r.vendor.tin_masked,
                _("Yes") if r.w9_on_file else _("No"),
                r.paid,
                r.by_processor,
            ]
            for r in t.rows
        ],
        totals=[_("Totals"), "", "", "", t.total, t.by_processor],
        pii=True,
    )
