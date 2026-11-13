"""Money hub > Dues & ledger: record payments, voids, refunds and adjustments (FIN-05/11).

Reading needs ``finance.read``; every write needs ``finance.manage``.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.billing.models import Adjustment
from homeoa.billing.services import adjustments as adj
from homeoa.changes.replay import date_value, money_change, money_value
from homeoa.core.context import association_today
from homeoa.ledger.services import unit_balance
from homeoa.registry.models import Unit
from homeoa.ui.nav import get_hub

from . import services as pay
from .forms import AdjustmentForm, ReceiptForm, RefundForm, VoidForm
from .models import Receipt


def _shell(title):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "ledger"}
        for i, t in enumerate(hub.subtabs)
    ]
    current = next((t for t in hub.subtabs if t.slug == "ledger"), None)
    return {"current_hub": hub, "subtabs": tabs, "current_tab": current, "heading": title}


def _can_manage(request):
    return has_perm(request.user, perms.FINANCE_MANAGE, association=request.association)


def _unit_summary(unit):
    if unit is None:
        return None
    return {
        "unit": unit,
        "balance": unit_balance(unit.pk, association=unit.association),
        "charges": pay.open_charges(unit),
        "credit": pay.unit_credit(unit.pk, association=unit.association),
    }


def _ledger_rows(association):
    from homeoa.registry import services as registry

    on = association_today()
    rows = []
    for unit in sorted(Unit.objects.all(), key=lambda u: u.sort_key):
        charges = pay.open_charges(unit)
        balance = unit_balance(unit.pk, association=association)
        rows.append(
            {
                "unit": unit,
                "owners": registry.current_owners(unit, on),
                "balance": balance,
                "balance_css": "num strong" if balance > 0 else "num",
                "credit": pay.unit_credit(unit.pk, association=association),
                "oldest_due": min((c.due_date for c in charges), default=None),
            }
        )
    return rows


@require_perm(perms.FINANCE_READ)
def ledger_home(request):
    """Money > Dues & ledger landing: unit balances plus links into the section."""
    ctx = _shell(_("Dues & ledger"))
    ctx.update({"rows": _ledger_rows(request.association), "can_manage": _can_manage(request)})
    return render(request, "payments/ledger_home.html", ctx)


@require_perm(perms.FINANCE_READ)
def index(request):
    ctx = _shell(_("Payments"))
    ctx.update(
        {
            "receipts": Receipt.objects.select_related("unit")[:50],
            "can_manage": _can_manage(request),
        }
    )
    return render(request, "payments/index.html", ctx)


def _unit_label(pk) -> str:
    unit = Unit.objects.filter(pk=pk).first() if str(pk or "").isdigit() else None
    return unit.number if unit else "?"


def _describe_receipt(request):
    p = request.POST
    unit = _unit_label(p.get("unit"))
    summary = _("Record a %(amount)s payment from Unit %(unit)s") % {
        "amount": money_value(p.get("amount")),
        "unit": unit,
    }
    details = [
        (_("Unit"), unit),
        (_("Amount"), money_value(p.get("amount"))),
        (_("Received"), date_value(p.get("received_date"))),
        (_("Method"), p.get("method", "")),
        (_("Payer"), p.get("payer", "")),
        (_("Reference"), p.get("reference", "")),
        (_("Memo"), p.get("memo", "")),
    ]
    return summary, [(k, v) for k, v in details if v]


def _describe_receipt_action(verb):
    def describe(request, pk):
        receipt = Receipt.objects.select_related("unit").filter(pk=pk).first()
        label = (
            _("%(amount)s from Unit %(unit)s")
            % {"amount": f"${receipt.amount:,.2f}", "unit": receipt.unit.number}
            if receipt
            else f"#{pk}"
        )
        details = [(_("Payment"), label)]
        for field, title in (("amount", _("Amount")), ("date", _("Date")), ("reason", _("Reason"))):
            if request.POST.get(field):
                value = request.POST[field]
                if field == "amount":
                    value = money_value(value)
                elif field == "date":
                    value = date_value(value)
                details.append((title, value))
        return str(verb) % {"payment": label}, details

    return describe


@money_change("money.receipt", _l("Record a payment"), describe=_describe_receipt)
@require_perm(perms.FINANCE_MANAGE)
def new(request):
    association = request.association
    form = ReceiptForm(
        request.POST or None,
        association=association,
        initial={
            "received_date": association_today(association),
            "method": "check",
            "unit": request.GET.get("unit"),
        },
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            receipt = pay.record_receipt(
                unit=d["unit"],
                amount=d["amount"],
                received_date=d["received_date"],
                method=d["method"],
                deposit_account=d["deposit_account"],
                payer=d["payer"],
                reference=d["reference"],
                memo=d["memo"],
                posted_by=request.user,
            )
        except pay.PaymentError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request,
                _("Recorded %(a)s from Unit %(u)s.")
                % {"a": f"${receipt.amount:,.2f}", "u": receipt.unit.number},
            )
            if request.headers.get("HX-Request"):
                return render(request, "payments/_result.html", _detail_ctx(request, receipt))
            return redirect("payments:detail", pk=receipt.pk)
    unit = None
    unit_id = form["unit"].value()
    if unit_id:
        unit = Unit.objects.filter(pk=unit_id).first() if str(unit_id).isdigit() else None
    ctx = _shell(_("Record payment"))
    ctx.update({"form": form, "summary": _unit_summary(unit)})
    status = 400 if request.method == "POST" and not request.headers.get("HX-Request") else 200
    return render(request, "payments/new.html", ctx, status=status)


@require_perm(perms.FINANCE_READ)
def unit_summary(request):
    """htmx partial: the chosen unit's balance and open charges."""
    unit_id = request.GET.get("unit", "")
    unit = Unit.objects.filter(pk=unit_id).first() if unit_id.isdigit() else None
    return render(request, "payments/_unit_summary.html", {"summary": _unit_summary(unit)})


def _detail_ctx(request, receipt):
    ctx = _shell(_("Payment"))
    ctx.update(
        {
            "receipt": receipt,
            "allocations": receipt.allocations.select_related("charge").order_by("pk"),
            "unapplied": pay.unapplied(receipt),
            "balance": unit_balance(receipt.unit_id, association=receipt.association),
            "refunds": receipt.refunds.all(),
            "can_manage": _can_manage(request),
            "void_form": VoidForm(),
            "refund_form": RefundForm(
                initial={
                    "date": association_today(receipt.association),
                    "method": receipt.method,
                    "amount": pay.unapplied(receipt) or None,
                }
            ),
        }
    )
    return ctx


@require_perm(perms.FINANCE_READ)
def detail(request, pk):
    receipt = get_object_or_404(Receipt.objects.select_related("unit", "deposit_account"), pk=pk)
    return render(request, "payments/detail.html", _detail_ctx(request, receipt))


@require_POST
@money_change(
    "money.receipt_void",
    _l("Void a payment"),
    describe=_describe_receipt_action(_l("Void the payment of %(payment)s")),
)
@require_perm(perms.FINANCE_MANAGE)
def void(request, pk):
    receipt = get_object_or_404(Receipt, pk=pk)
    form = VoidForm(request.POST)
    if form.is_valid():
        try:
            pay.void_receipt(receipt, reason=form.cleaned_data["reason"], posted_by=request.user)
        except pay.PaymentError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Payment voided."))
    else:
        messages.error(request, _("Give a reason for voiding."))
    return redirect("payments:detail", pk=pk)


@money_change(
    "money.refund",
    _l("Record a refund"),
    describe=_describe_receipt_action(_l("Record a refund of the payment of %(payment)s")),
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def refund(request, pk):
    receipt = get_object_or_404(Receipt, pk=pk)
    form = RefundForm(request.POST)
    if form.is_valid():
        try:
            pay.record_refund(receipt, posted_by=request.user, **form.cleaned_data)
        except pay.PaymentError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Refund recorded. HomeOA did not send any money."))
    else:
        messages.error(request, _("Check the refund amount, date and reason."))
    return redirect("payments:detail", pk=pk)


def _describe_adjustment(request):
    p = request.POST
    kinds = dict(AdjustmentForm.KINDS)
    kind = str(kinds.get(p.get("kind"), p.get("kind", "")))
    unit = _unit_label(p.get("unit"))
    amount = money_value(p.get("amount")) if p.get("amount") else ""
    summary = _("%(kind)s for Unit %(unit)s") % {"kind": kind, "unit": unit}
    if amount:
        summary = _("%(kind)s of %(amount)s for Unit %(unit)s") % {
            "kind": kind,
            "amount": amount,
            "unit": unit,
        }
    details = [
        (_("Type"), kind),
        (_("Unit"), unit),
        (_("Amount"), amount),
        (_("Date"), date_value(p.get("date"))),
        (_("Description"), p.get("description", "")),
        (_("Reason"), p.get("reason", "")),
    ]
    return summary, [(k, v) for k, v in details if v]


# Credits, write-offs, manual charges and voided charges always need a second board member.
@money_change(
    "money.adjustment",
    _l("Credit, write-off or charge"),
    always=True,
    describe=_describe_adjustment,
)
@require_perm(perms.FINANCE_READ)
def adjustments(request):
    association = request.association
    can_manage = _can_manage(request)
    form = None
    if can_manage:
        form = AdjustmentForm(
            request.POST or None,
            association=association,
            initial={"date": association_today(association)},
        )
    if request.method == "POST":
        if not can_manage:
            raise PermissionDenied
        if form.is_valid():
            d = form.cleaned_data
            try:
                result = _apply_adjustment(d, request.user)
            except pay.PaymentError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, _("Adjustment saved."))
                unit = d["unit"]
                return redirect(f"{request.path}?unit={unit.pk}&saved={result.pk}")
    ctx = _shell(_("Adjustments"))
    unit_id = request.GET.get("unit", "")
    unit = Unit.objects.filter(pk=unit_id).first() if unit_id.isdigit() else None
    ctx.update(
        {
            "form": form,
            "adjustments": Adjustment.objects.select_related("unit", "account")[:50],
            "summary": _unit_summary(unit),
        }
    )
    return render(
        request,
        "payments/adjustments.html",
        ctx,
        status=400 if request.method == "POST" else 200,
    )


def _apply_adjustment(d, user):
    kind, unit, reason = d["kind"], d["unit"], d["reason"]
    common = {"date": d["date"], "posted_by": user}
    if kind == "credit_memo":
        return adj.credit_memo(unit, d["amount"], reason, charge=d["charge"], **common)
    if kind == "write_off":
        return adj.write_off(unit, d["amount"], reason, charge=d["charge"], **common)
    if kind == "manual_charge":
        return adj.manual_charge(unit, d["amount"], reason, d["description"], **common)
    return adj.void_charge(d["charge"], reason, **common)
