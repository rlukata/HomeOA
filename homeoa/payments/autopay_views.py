"""Autopay pages: Money > Payments > Autopay and its follow-ups for the board, and the
owner's autopay page under Pay."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.access.services import today_for
from homeoa.core.context import association_today
from homeoa.portal.pay import _amount, pay_context
from homeoa.portal.pay import _back as pay_back
from homeoa.portal.views.base import _render

from .models import AutopayRule
from .services import PaymentError
from .views import _can_manage, _shell


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def autopay_ack(request, pk):
    """Clear the "Autopay … failed" to-do once the board has followed up."""
    from . import autopay
    from .models import AutopayCharge

    charge = get_object_or_404(AutopayCharge.objects, pk=pk)
    autopay.acknowledge(charge, user=request.user)
    messages.success(request, _("Noted. The failed autopay is off the to-do list."))
    return redirect(_back(request))


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def autopay_resend(request, pk):
    """Resend a failed pre-debit notice and reschedule the debit."""
    from . import autopay
    from .models import AutopayCharge, AutopayStatus

    charge = get_object_or_404(AutopayCharge.objects, pk=pk)
    try:
        charge = autopay.resend_notice(
            charge, as_of=association_today(request.association), user=request.user
        )
    except autopay.ResendError as exc:
        messages.error(request, str(exc))
    else:
        if charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED:
            messages.error(
                request,
                _("The notice could not be sent again, so the debit is still not scheduled."),
            )
        else:
            messages.success(
                request,
                _("Notice sent. Autopay will debit up to %(amount)s on %(date)s.")
                % {
                    "amount": f"${charge.noticed_amount:,.2f}",
                    "date": date_format(charge.debit_on, "M j, Y"),
                },
            )
    return redirect(_back(request))


def _back(request) -> str:
    """Where an autopay follow-up returns: the autopay page when posted from there."""
    if request.POST.get("next") == "settings":
        return "payments:autopay_settings"
    return "dashboard:index"


@require_perm(perms.FINANCE_READ)
def autopay_settings(request):
    """Money > Payments > Autopay: the debit day and notice lead."""
    from . import autopay
    from .forms import AutopaySettingsForm

    association = request.association
    today = association_today(association)
    month, notice_on, debit_on = autopay.next_cycle(association, today)
    day, lead = autopay.cycle_settings(association, month)
    least = autopay.notice_days(association, today)
    form = AutopaySettingsForm(
        request.POST if request.method == "POST" else None,
        least=least,
        most=autopay.MAX_LEAD_DAYS,
        initial={"debit_day": day, "notice_lead_days": lead},
    )
    if request.method == "POST":
        if not _can_manage(request):
            raise PermissionDenied
        if form.is_valid():
            try:
                start = autopay.set_cycle_settings(
                    association,
                    debit_day=form.cleaned_data["debit_day"],
                    notice_lead_days=form.cleaned_data["notice_lead_days"],
                    as_of=today,
                    user=request.user,
                )
            except autopay.SettingsError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    _("Autopay settings saved. They apply from the %(month)s debit.")
                    % {"month": date_format(start, "F Y")},
                )
                return redirect("payments:autopay_settings")
    ctx = _shell(_("Autopay"))
    ctx.update(
        {
            "form": form,
            "can_manage": _can_manage(request),
            "least": least,
            "next_notice_on": notice_on,
            "next_debit_on": debit_on,
            "history": autopay.history(association),
        }
    )
    return render(
        request,
        "payments/autopay_settings.html",
        ctx,
        status=400 if request.method == "POST" else 200,
    )


@resident_view(perm=perms.BALANCE_OWN)
def autopay_view(request):
    """GET: autopay settings. POST ``action=on``: enroll; ``action=off``: turn off."""
    from homeoa.payments import autopay

    ctx = pay_context(request, request.POST.get("unit") or request.GET.get("unit"))
    if ctx is None:
        raise Http404
    ctx.enrollment = autopay.active_enrollment(ctx.period)
    today = today_for(request.association)
    form = {"rule": AutopayRule.BALANCE, "fixed_amount": "", "method": ""}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "off" and ctx.enrollment is not None:
            autopay.cancel(ctx.enrollment, user=request.user)
            messages.success(request, _("Autopay is off."))
            return redirect(pay_back(ctx))
        if action == "on":
            form = {k: request.POST.get(k, "") for k in form}
            try:
                fixed = None
                if form["rule"] == AutopayRule.FIXED:
                    fixed = _amount(form["fixed_amount"])
                enrollment = autopay.enroll(
                    ctx.unit,
                    ctx.person,
                    rule=form["rule"],
                    fixed_amount=fixed,
                    mandate_ref=form["method"],
                    user=request.user,
                    as_of=today,
                )
            except PaymentError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    _("Autopay is on from %(date)s.")
                    % {"date": date_format(enrollment.starts_on, "M j")},
                )
                return redirect(pay_back(ctx))
    return _render(
        request,
        "pay",
        "portal/autopay.html",
        pay=ctx,
        form=form,
        rules=AutopayRule.choices,
        starts_on=autopay.next_month(today),
        history=autopay.history(request.association, ownership_period=ctx.period),
    )
