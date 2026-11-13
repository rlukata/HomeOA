"""Money hub > Close (FIN-15): month checklist and close, year-end close, reopen."""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.changes.replay import money_change, money_page
from homeoa.core.context import association_today
from homeoa.ledger import services as ledger
from homeoa.ui.nav import get_hub

from . import services
from .models import Close


def _shell(title):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "close"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "close"), None)
    return {"current_hub": hub, "subtabs": tabs, "current_tab": current, "heading": title}


def _month(value, default):
    try:
        year, month = (int(p) for p in value.split("-"))
        services.month_bounds(year, month)
        return year, month
    except ValueError, AttributeError:
        return default


def _year(value, default):
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _defaults(association):
    """The month after the lock (else last month) and the last fiscal year."""
    today = association_today(association)
    locked = ledger.get_locked_through(association)
    if locked is not None:
        nxt = locked.replace(day=1)
        nxt = nxt.replace(year=nxt.year + (nxt.month == 12), month=nxt.month % 12 + 1)
        month = (nxt.year, nxt.month)
    else:
        month = (today.year - (today.month == 1), (today.month - 2) % 12 + 1)
    fy_month = association.fiscal_year_start_month or 1
    fy_start_year = today.year if today.month >= fy_month else today.year - 1
    return month, fy_start_year - 1


# Other board members see the close buttons too; what they submit goes for approval.
@money_page
@require_perm(perms.FINANCE_READ)
def index(request):
    association = request.association
    default_month, default_year = _defaults(association)
    year, month = _month(request.GET.get("month"), default_month)
    fy = _year(request.GET.get("year"), default_year)
    m_start, m_end = services.month_bounds(year, month)
    y_start, y_end = services.fiscal_year_bounds(association, fy)
    ctx = _shell(_("Period close"))
    ctx.update(
        locked_through=ledger.get_locked_through(association),
        month_value=f"{year:04d}-{month:02d}",
        month_start=m_start,
        month_end=m_end,
        month_checks=services.checklist(association, m_start, m_end),
        fy=fy,
        year_start=y_start,
        year_end=y_end,
        year_checks=services.checklist(association, y_start, y_end),
        closes=Close.objects.select_related("closed_by", "reopened_by", "entry"),
        can_manage=has_perm(request.user, perms.FINANCE_MANAGE, association=association),
    )
    ctx["month_ready"] = all(c.ok for c in ctx["month_checks"])
    ctx["year_ready"] = all(c.ok for c in ctx["year_checks"])
    return render(request, "closing/index.html", ctx)


def _describe_close_month(request):
    month = request.POST.get("month", "")
    return _("Close the books for %(m)s") % {"m": month}, [(_("Month"), month)]


def _describe_close_year(request):
    year = request.POST.get("year", "")
    return _("Close fiscal year %(y)s") % {"y": year}, [(_("Fiscal year"), year)]


def _describe_reopen(request, pk):
    close = Close.objects.filter(pk=pk).first()
    label = f"{close.period_start} – {close.period_end}" if close else f"#{pk}"
    return (
        _("Reopen the closed books for %(p)s") % {"p": label},
        [(_("Period"), label), (_("Reason"), request.POST.get("reason", ""))],
    )


@money_change("money.close_month", _l("Close a month"), describe=_describe_close_month)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def close_month_view(request):
    year, month = _month(request.POST.get("month"), (None, None))
    if year is None:
        messages.error(request, _("Pick a month to close."))
        return redirect("closing:index")
    try:
        close = services.close_month(request.association, year, month, user=request.user)
    except services.CloseError as exc:
        messages.error(request, str(exc))
        return redirect(f"{_index()}?month={year:04d}-{month:02d}")
    messages.success(
        request, _("Closed. The books are locked through %(d)s.") % {"d": close.period_end}
    )
    return redirect("closing:index")


@money_change("money.close_year", _l("Close a fiscal year"), describe=_describe_close_year)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def close_year_view(request):
    fy = _year(request.POST.get("year"), None)
    if fy is None:
        messages.error(request, _("Pick a year to close."))
        return redirect("closing:index")
    try:
        close = services.close_year(request.association, fy, user=request.user)
    except services.CloseError as exc:
        messages.error(request, str(exc))
        return redirect(f"{_index()}?year={fy}")
    messages.success(
        request,
        _("Year closed. Net income moved into fund balance; locked through %(d)s.")
        % {"d": close.period_end},
    )
    return redirect("closing:index")


# Reopening closed books always needs a second board member.
@money_change("money.reopen", _l("Reopen closed books"), always=True, describe=_describe_reopen)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def reopen_view(request, pk):
    close = get_object_or_404(Close, pk=pk)
    try:
        reopened = services.reopen(close, reason=request.POST.get("reason", ""), user=request.user)
    except services.CloseError as exc:
        messages.error(request, str(exc))
        return redirect("closing:index")
    messages.success(request, _("Reopened %(n)s period(s).") % {"n": len(reopened)})
    return redirect("closing:index")


def _index():
    return reverse("closing:index")
