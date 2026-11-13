"""Statements tab (POR-03): the owner's monthly statements with PDF download (own only)."""

from __future__ import annotations

from django.http import Http404, HttpResponse

from homeoa.access import permissions as perms
from homeoa.access.decorators import resident_view

from .base import _render
from .mine import my_periods


def _mine(request):
    from homeoa.statements.statement_models import Statement

    # Residents only ever see statements that were actually sent; previews stay staff-only.
    return Statement.objects.filter(
        ownership_period__in=my_periods(request), status=Statement.Status.SENT
    ).select_related("unit")


@resident_view(perm=perms.BALANCE_OWN)
def statements(request):
    from homeoa.statements.owner_statements import statement_autopay_text

    items = list(_mine(request).order_by("-period_start", "unit__number"))
    for st in items:
        # Same autopay sentence as the PDF and email (FIN-06); empty when no debit is scheduled.
        st.autopay_line = statement_autopay_text(st)
    return _render(request, "pay", "portal/statements.html", statements=items)


@resident_view(perm=perms.BALANCE_OWN)
def statement_pdf(request, pk):
    from homeoa.letters import services as letter_services

    statement = _mine(request).filter(pk=pk).first()
    if statement is None:
        raise Http404
    data = letter_services.letter_bytes(statement.letter)
    resp = HttpResponse(data, content_type="application/pdf")
    name = f"statement-{statement.unit.number}-{statement.period_end}.pdf"
    resp["Content-Disposition"] = f'inline; filename="{name}"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp
