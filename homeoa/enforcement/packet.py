"""The open violations section of the monthly board packet."""

from __future__ import annotations

from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from homeoa.reports.export import DATE, Column, TableSpec
from homeoa.reports.sections import PacketContext, PacketPart


def open_violations(ctx: PacketContext) -> PacketPart:
    from homeoa.enforcement.models import Violation

    violations = list(
        Violation.all_associations.filter(association=ctx.association, status=Violation.Status.OPEN)
        .select_related("unit", "category")
        .order_by("observed_on")
    )
    table = TableSpec(
        title=_("Open violations"),
        columns=[Column(_("Unit")), Column(_("Category")), Column(_("Observed"), DATE)],
        rows=[[v.unit.number, str(v.category), v.observed_on] for v in violations],
    )
    n = len(violations)
    sentence = (
        ngettext("%(n)s violation is open.", "%(n)s violations are open.", n) % {"n": n}
        if n
        else _("No violations are open.")
    )
    return PacketPart([table], [[_("Open violations"), n]], [sentence])
