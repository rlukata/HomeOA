"""Money > Statements: generate a month, see each unit's channel and status, open PDFs."""

import datetime as dt

from django.contrib import messages
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa import providers
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.letters import batches
from homeoa.letters import services as letter_services
from homeoa.letters.models import MailBatch
from homeoa.ui.nav import get_hub

from . import owner_statements
from .statement_models import Statement, StatementRun

MONTHS_SHOWN = 6


def _shell(title):
    """Statements live under Money > Dues & ledger (no own section in the prototype)."""
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "ledger"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "ledger"), None)
    return {"current_hub": hub, "subtabs": tabs, "current_tab": current, "heading": title}


def _recent_months(today: dt.date, n: int = MONTHS_SHOWN) -> list[dt.date]:
    months, m = [], today.replace(day=1)
    for _i in range(n):
        months.append(m)
        m = (m - dt.timedelta(days=1)).replace(day=1)
    return months


def _can_manage(request) -> bool:
    return has_perm(request.user, perms.FINANCE_MANAGE, association=request.association)


@require_perm(perms.FINANCE_READ)
def index(request):
    runs = {
        r.period_start: r
        for r in StatementRun.objects.annotate(
            n=Count("statements"), total=Sum("statements__closing")
        )
    }
    today = association_today(request.association)
    months = [{"month": m, "run": runs.pop(m, None)} for m in _recent_months(today)]
    older = sorted(runs.values(), key=lambda r: r.period_start, reverse=True)
    ctx = _shell(_("Statements"))
    ctx.update(
        {
            "months": months,
            "older": older,
            "can_manage": _can_manage(request),
            "residents_on": residents_enabled(request.association),
        }
    )
    return render(request, "statements/index.html", ctx)


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def generate(request):
    try:
        month = dt.date.fromisoformat(request.POST.get("month", "") + "-01")
    except ValueError:
        messages.error(request, _("Choose a month."))
        return redirect("statements:index")
    run = owner_statements.generate(request.association, month, user=request.user)
    messages.success(
        request,
        _("Generated %(n)s statements. They are previews; nothing was sent.")
        % {"n": run.statements.count()},
    )
    return redirect("statements:statement_run", pk=run.pk)


@require_perm(perms.FINANCE_READ)
def run_detail(request, pk):
    run = get_object_or_404(StatementRun, pk=pk)
    statements = list(
        run.statements.select_related("unit", "mail_batch").order_by("unit__number", "period_start")
    )
    statements.sort(key=lambda s: (s.unit.sort_key, s.period_start))
    batch_ids = {s.mail_batch_id for s in statements if s.mail_batch_id}
    mail_batches = MailBatch.objects.filter(pk__in=batch_ids).annotate(n=Count("items"))
    ctx = _shell(_("Statements · %(month)s") % {"month": run.period_start.strftime("%B %Y")})
    ctx.update(
        {
            "run": run,
            "statements": statements,
            "mail_batches": mail_batches,
            "provider": providers.plugin_for("PostalProvider", request.association)
            or _("Print batch (mail by hand)"),
            "can_manage": _can_manage(request),
            "can_letters": has_perm(
                request.user, perms.LETTERS_MANAGE, association=request.association
            ),
            "residents_on": residents_enabled(request.association),
            "unsent": any(s.status != Statement.Status.SENT for s in statements),
        }
    )
    return render(request, "statements/run.html", ctx)


def _refused(request, message: str, status: int = 400):
    if request.headers.get("HX-Request"):
        return HttpResponse(message, status=status)
    ctx = {**_shell(_("Statements")), "message": message}
    return render(request, "statements/refused.html", ctx, status=status)


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def send_run(request, pk):
    run = get_object_or_404(StatementRun, pk=pk)
    try:
        sent = owner_statements.send_run(run, by=request.user)
    except owner_statements.StatementError as exc:
        return _refused(request, str(exc))
    if sent:
        messages.success(request, _("Sent %(n)s statements.") % {"n": len(sent)})
    else:
        messages.info(request, _("Every statement in this run was already sent."))
    return redirect("statements:statement_run", pk=run.pk)


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def send_statement(request, pk):
    statement = get_object_or_404(Statement, pk=pk)
    was_sent = statement.status == Statement.Status.SENT
    try:
        statement = owner_statements.send_statement(statement, by=request.user)
    except owner_statements.StatementError as exc:
        return _refused(request, str(exc))
    if was_sent:
        messages.info(request, _("This statement was already sent."))
    else:
        messages.success(
            request,
            _("Statement for unit %(unit)s sent (%(channel)s).")
            % {"unit": statement.unit.number, "channel": statement.get_channel_display()},
        )
    nxt = request.POST.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect("statements:statement_run", pk=statement.run_id)


def _pdf(data: bytes, filename: str) -> HttpResponse:
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{filename}"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp


@require_perm(perms.FINANCE_READ)
def statement_pdf(request, pk):
    statement = get_object_or_404(Statement, pk=pk)
    data = letter_services.letter_bytes(statement.letter)
    return _pdf(data, f"statement-{statement.unit.number}-{statement.period_end}.pdf")


@require_perm(perms.FINANCE_READ)
def batch_pdf(request, pk, batch_pk):
    run = get_object_or_404(StatementRun, pk=pk)
    if not run.statements.filter(mail_batch_id=batch_pk).exists():
        raise Http404
    batch = get_object_or_404(MailBatch, pk=batch_pk)
    return _pdf(batches.file_bytes(batch, "pdf"), f"mail-batch-{batch.pk}.pdf")
