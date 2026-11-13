"""Core unit-file contributors: ledger, generated letters, statements, comm log.

Each one gates itself on the permission the linked page needs, so the file never lists
something the viewer cannot open.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm

from . import unitfile
from .unitfile import Entry


def _can(request, perm) -> bool:
    return has_perm(request.user, perm, association=getattr(request, "association", None))


def ledger(unit, request):
    """One entry: the unit ledger, dated by its latest posting, with the balance."""
    if not _can(request, perms.FINANCE_READ):
        return []
    from homeoa.billing.owner_ledger import owner_ledger
    from homeoa.letters.merge import money

    rows = owner_ledger(unit)
    if not rows:
        return []
    last = rows[-1]
    return [
        Entry(
            date=last.date,
            kind="ledger",
            kind_label=gettext_lazy("Ledger"),
            title=_("Unit ledger (%(n)s entries)") % {"n": len(rows)},
            url=reverse("registry:unit", args=[unit.number]) + "#ledger-h",
            detail=_("Balance %(amount)s") % {"amount": money(last.balance)},
        )
    ]


def letters(unit, request):
    """Generated letters, except statement PDFs (the statements contributor lists those)."""
    if not _can(request, perms.UNITS_READ):
        return []
    from homeoa.letters.models import Letter
    from homeoa.statements.statement_models import Statement

    stmt_letters = Statement.all_associations.filter(unit=unit).values("letter_id")
    qs = (
        Letter.all_associations.filter(association_id=unit.association_id, unit=unit)
        .exclude(pk__in=stmt_letters)
        .select_related("template_version__template")
    )
    return [
        Entry(
            date=letter.as_of,
            kind="letters",
            kind_label=gettext_lazy("Letter"),
            title=letter.subject or letter.template_version.template.name,
            url=reverse("letters:download", args=[letter.pk]),
            detail=letter.recipient_name,
        )
        for letter in qs
    ]


def statements(unit, request):
    if not _can(request, perms.FINANCE_READ):
        return []
    from homeoa.letters.merge import money
    from homeoa.statements.statement_models import Statement

    qs = Statement.all_associations.filter(association_id=unit.association_id, unit=unit)
    return [
        Entry(
            date=s.period_end,
            kind="statements",
            kind_label=gettext_lazy("Statement"),
            title=_("Statement %(start)s – %(end)s")
            % {"start": s.period_start.isoformat(), "end": s.period_end.isoformat()},
            url=reverse("statements:pdf", args=[s.pk]),
            detail=_("%(status)s · closing balance %(amount)s")
            % {"status": s.get_status_display(), "amount": money(s.closing)},
        )
        for s in qs
    ]


def comms(unit, request):
    """Every delivery in the unit's comm log (email, letter, in-app, preview)."""
    from homeoa.notify import commlog
    from homeoa.notify.views import can_read_comms

    if not can_read_comms(request):
        return []
    url = reverse("notify:unit_comms", args=[unit.number])
    return [
        Entry(
            date=timezone.localdate(commlog.last_event_at(d)),
            kind="comms",
            kind_label=gettext_lazy("Message"),
            title=d.notification.subject or d.notification.type_key,
            url=url,
            detail=" · ".join(
                str(x)
                for x in (
                    d.notification.recipient.full_name,
                    commlog.channel_label(d.channel),
                    d.get_status_display(),
                )
            ),
        )
        for d in commlog.entries(unit)
    ]


def ledger_exports(unit, request):
    """Mediation ledger exports: PDF link, CSV in the detail."""
    if not _can(request, perms.FINANCE_READ):
        return []
    from homeoa.letters.merge import money

    from .models import LedgerExport

    qs = LedgerExport.all_associations.filter(association_id=unit.association_id, unit=unit)
    return [
        Entry(
            date=x.as_of,
            kind="ledger_exports",
            kind_label=gettext_lazy("Ledger export"),
            title=_("Itemized ledger %(start)s – %(end)s (PDF)")
            % {"start": x.start.isoformat(), "end": x.as_of.isoformat()},
            url=reverse("documents:ledger_export_file", args=[x.pk, "pdf"]),
            detail=_("Balance %(amount)s") % {"amount": money(x.balance)},
            extra_url=reverse("documents:ledger_export_file", args=[x.pk, "csv"]),
            extra_label=gettext_lazy("CSV"),
        )
        for x in qs
    ]


def register_defaults() -> None:
    unitfile.register("ledger", ledger)
    unitfile.register("ledger_exports", ledger_exports)
    unitfile.register("letters", letters)
    unitfile.register("statements", statements)
    unitfile.register("comms", comms)
