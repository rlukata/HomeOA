from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.providers import get_provider
from homeoa.registry.models import Unit

from . import mediation, unitfile
from .models import LedgerExport


def can_read_unit_file(request) -> bool:
    """Board only (board shell + units.read); owners get their own via the portal later."""
    assoc = getattr(request, "association", None)
    return has_perm(request.user, perms.BOARD_SHELL, association=assoc) and has_perm(
        request.user, perms.UNITS_READ, association=assoc
    )


@require_perm(perms.UNITS_READ)
def unit_file(request, number):
    if not can_read_unit_file(request):
        raise PermissionDenied
    unit = get_object_or_404(Unit.objects, number=number)
    kind = request.GET.get("kind", "")
    if kind not in unitfile.registered():
        kind = ""
    all_entries = unitfile.entries(unit, request)
    kinds = {}
    for e in all_entries:
        kinds.setdefault(e.kind, e.kind_label)
    rows = [e for e in all_entries if not kind or e.kind == kind]
    return render(
        request,
        "documents/unit_file.html",
        {
            "unit": unit,
            "rows": rows,
            "kind": kind,
            "kinds": list(kinds.items()),
            "can_export": has_perm(
                request.user, perms.FINANCE_MANAGE, association=request.association
            ),
        },
    )


def _board_finance(request, perm) -> None:
    if not (
        can_read_unit_file(request)
        and has_perm(request.user, perm, association=request.association)
    ):
        raise PermissionDenied


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def ledger_export(request, number):
    """Generate the mediation ledger export (PDF + CSV) and file it in the unit file."""
    _board_finance(request, perms.FINANCE_MANAGE)
    unit = get_object_or_404(Unit.objects, number=number)
    try:
        rec = mediation.export(unit, user=request.user)
    except mediation.NoOwnershipPeriod:
        messages.error(request, _("This unit has no current owner of record to export."))
        return redirect("documents:unit_file", number=unit.number)
    messages.success(
        request,
        _("Ledger exported from %(start)s to %(end)s.")
        % {"start": rec.start.isoformat(), "end": rec.as_of.isoformat()},
    )
    return redirect("documents:unit_file", number=unit.number)


@require_perm(perms.FINANCE_READ)
def ledger_export_file(request, pk, fmt):
    _board_finance(request, perms.FINANCE_READ)
    if fmt not in ("pdf", "csv"):
        raise Http404
    rec = get_object_or_404(LedgerExport.objects.select_related("unit"), pk=pk)
    key, ctype = (rec.pdf_key, "application/pdf") if fmt == "pdf" else (rec.csv_key, "text/csv")
    data = get_provider("FileStorage", request.association).get(key)
    resp = HttpResponse(data, content_type=ctype)
    disp = "inline" if fmt == "pdf" else "attachment"
    name = f"ledger-{rec.unit.number}-{rec.as_of.isoformat()}.{fmt}"
    resp["Content-Disposition"] = f'{disp}; filename="{name}"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp
