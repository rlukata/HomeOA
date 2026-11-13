"""HTTP side of the export framework (RPT-01); formats live in ``homeoa.reports.export``.

Usage in a view::

    fmt = export_format(request)
    if fmt:
        spec = TableSpec(_("Units"), [Column(_("Unit")), Column(_("Balance"), MONEY)], rows)
        return export_response(request, spec, fmt)

``csv_response`` (the older helper) is kept and delegates to the same CSV writer.
Templates link to the three formats with ``{% include "ui/_export_links.html" %}``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from django.http import HttpResponse

from homeoa.reports import export as fmt_mod
from homeoa.reports.export import Column, TableSpec

cell = fmt_mod.text_cell


def export_format(request) -> str | None:
    value = request.GET.get("format", "")
    return value if value in fmt_mod.FORMATS else None


def wants_csv(request) -> bool:
    return request.GET.get("format") == fmt_mod.CSV


def _response(body: bytes, fmt: str, name: str) -> HttpResponse:
    response = HttpResponse(body, content_type=fmt_mod.CONTENT_TYPES[fmt])
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    return response


def export_response(request, spec: TableSpec, fmt: str, *, audit_obj=None) -> HttpResponse:
    """Render ``spec`` as ``fmt`` and download it; audits exports of tables with PII."""
    from homeoa.core.context import association_today

    association = getattr(request, "association", None)
    on = association_today(association) if association is not None else None
    body = fmt_mod.render(spec, fmt, association=association, on=on)
    name = fmt_mod.filename(spec, fmt, association=association, on=on)
    if spec.pii:
        from homeoa import audit

        audit.record(
            "reports.exported",
            audit_obj,
            association=association,
            actor=getattr(request, "user", None),
            object_type=None if audit_obj is not None else "reports.export",
            object_repr=None if audit_obj is not None else str(spec.title),
            changes={"format": fmt, "file": name, "rows": len(spec.rows)},
            message=str(spec.title),
        )
    return _response(body, fmt, name)


def csv_response(filename: str, headers: Sequence, rows: Iterable[Sequence]) -> HttpResponse:
    """Plain CSV, filename as given. Same bytes as ``to_csv``."""
    spec = TableSpec(title=filename, columns=[Column(str(h)) for h in headers], rows=list(rows))
    name = (fmt_mod.safe_name(filename) or "export") + ".csv"
    return _response(fmt_mod.to_csv(spec).encode("utf-8"), fmt_mod.CSV, name)
