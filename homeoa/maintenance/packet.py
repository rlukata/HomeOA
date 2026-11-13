"""The open work orders section of the monthly board packet."""

from __future__ import annotations

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from homeoa.reports.export import DATE, Column, TableSpec
from homeoa.reports.sections import PacketContext, PacketPart


def open_work_orders(ctx: PacketContext) -> PacketPart:
    from homeoa.maintenance.models import WorkOrder

    work_orders = list(
        WorkOrder.all_associations.filter(association=ctx.association, status__in=WorkOrder.OPEN)
        .select_related("vendor")
        .order_by("scheduled_for", "pk")
    )
    table = TableSpec(
        title=_("Open work orders"),
        columns=[
            Column(_("Work order")),
            Column(_("Vendor")),
            Column(_("Status")),
            Column(_("Scheduled"), DATE),
        ],
        rows=[
            [f"#{wo.pk}", wo.vendor.name, wo.get_status_display(), wo.scheduled_for]
            for wo in work_orders
        ],
    )
    n = len(work_orders)
    sentence = (
        ngettext("%(n)s work order is open.", "%(n)s work orders are open.", n) % {"n": n}
        if n
        else _("No work orders are open.")
    )
    return PacketPart([table], [[_("Open work orders"), n]], [sentence])
