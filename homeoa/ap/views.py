"""Money hub > Bills: list, enter/edit drafts, detail, submit, void, mark paid (FIN-12).

Reads need ``finance.read``; every write needs ``finance.manage``.
"""

from __future__ import annotations

from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.changes.replay import change_view, date_value, money_change
from homeoa.core.context import association_today
from homeoa.core.fields import ZERO
from homeoa.ui.nav import get_hub

from . import prefill, services
from .approvals import services as approval_services
from .approvals.forms import ApprovalRuleForm, RejectForm
from .approvals.models import Approval, ApprovalRule
from .forms import AttachmentForm, BillForm, MarkPaidForm, VoidForm, line_formset
from .models import Bill, BillAttachment, BillLine

READ = (perms.FINANCE_READ, perms.FINANCE_MANAGE, perms.BILLS_ENTER)
ENTER = (perms.FINANCE_MANAGE, perms.BILLS_ENTER)

FILTERS = {
    "open": (Bill.Status.PENDING, Bill.Status.APPROVED),
    "draft": (Bill.Status.DRAFT,),
    "pending": (Bill.Status.PENDING,),
    "approved": (Bill.Status.APPROVED,),
    "paid": (Bill.Status.PAID,),
    "void": (Bill.Status.VOID,),
}

BADGES = {
    Bill.Status.DRAFT: "",
    Bill.Status.PENDING: "warn",
    Bill.Status.APPROVED: "accent",
    Bill.Status.PAID: "ok",
    Bill.Status.VOID: "",
}


def _render(request, template, context, status=200):
    hub = get_hub("money")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "bills"}
        for i, t in enumerate(hub.subtabs)
    ]
    ctx = {
        "current_hub": hub,
        "subtabs": tabs,
        "can_manage": has_perm(request.user, perms.FINANCE_MANAGE, association=request.association),
        "today": association_today(request.association),
        **context,
    }
    return render(request, template, ctx, status=status)


def _bills():
    return Bill.objects.select_related("vendor").prefetch_related(
        Prefetch("lines", queryset=BillLine.objects.select_related("account", "fund"))
    )


def _decorate(bills, today):
    for b in bills:
        b.badge = BADGES.get(b.status, "")
        b.overdue = b.is_overdue(today)
    return bills


@require_perm(*READ)
def index(request):
    status = request.GET.get("status", "open")
    if status not in FILTERS and status != "all":
        status = "open"
    qs = _bills()
    if status != "all":
        qs = qs.filter(status__in=FILTERS[status])
    today = association_today(request.association)
    bills = _decorate(list(qs), today)
    context = {
        "bills": bills,
        "status": status,
        "statuses": [
            ("open", _("Open")),
            ("draft", _("Draft")),
            ("pending", _("Pending approval")),
            ("approved", _("Approved")),
            ("paid", _("Paid")),
            ("void", _("Void")),
            ("all", _("All")),
        ],
        "total": sum((b.total for b in bills if b.status != Bill.Status.VOID), ZERO),
        "overdue_count": sum(1 for b in bills if b.overdue),
    }
    if not request.htmx:
        waiting = _decorate(list(_bills().filter(status=Bill.Status.PENDING)), today)
        for b in waiting:
            b.done, b.required = approval_services.progress(b)
            b.mine = approval_services.can_approve(b, request.user)
        context["waiting"] = waiting
        context["can_rules"] = has_perm(
            request.user, perms.POLICY_PROPOSE, association=request.association
        )
    template = "ap/_rows.html" if request.htmx else "ap/list.html"
    return _render(request, template, context)


def _bill(pk) -> Bill:
    return get_object_or_404(_bills().prefetch_related("attachments"), pk=pk)


@require_perm(*READ)
def detail(request, pk):
    bill = _bill(pk)
    _decorate([bill], association_today(request.association))
    funds = list(services.fund_totals(bill)) if bill.status == Bill.Status.APPROVED else []
    context = {
        **_approval_context(request, bill),
        "void_form": VoidForm(),
        "attach_form": AttachmentForm(),
        "paid_form": MarkPaidForm(
            funds=funds, initial={"paid_on": association_today(request.association)}
        ),
    }
    return _render(request, "ap/detail.html", context)


def _source_work_order(request):
    """The work order a new bill is created from (``?work_order=`` / hidden field), if any."""
    from homeoa.maintenance.models import WorkOrder

    raw = request.POST.get("work_order") or request.GET.get("work_order")
    try:
        return WorkOrder.objects.filter(pk=int(raw)).first() if raw else None
    except ValueError:
        return None


def _link_work_order(request, wo, bill) -> None:
    from homeoa.maintenance import services as maintenance

    try:
        maintenance.link_bill(wo, bill, user=request.user)
    except maintenance.MaintenanceError as exc:
        messages.warning(request, str(exc))


def _invoice_reader_on(request) -> bool:
    """Whether "Fill in from an invoice" is offered: the AI assistance feature is on and
    an AI provider is connected."""
    from homeoa.plugins import slots
    from homeoa.providers import get_provider

    association = request.association
    return slots.module_on("ai_assistance", association) and bool(
        get_provider("AiProvider", association).available
    )


def _edit(request, bill):
    creating = bill.pk is None
    wo = _source_work_order(request) if creating else None
    proposal = None
    if creating and not request.POST and request.GET.get("prefill"):
        proposal = prefill.take(request, request.GET["prefill"])
    held = (proposal or {}).get("file") or (request.POST.get("held_file", "") if creating else "")
    held_data = prefill.held_file(held, request.association) if held else None
    form = BillForm(request.POST or None, request.FILES or None, instance=bill)
    line_initial = None
    if wo is not None and not request.POST and wo.estimated_cost is not None:
        line_initial = [{"amount": wo.estimated_cost, "memo": str(wo)}]
    if proposal and proposal.get("lines"):
        line_initial = proposal["lines"]
    formset = line_formset(len(line_initial or []))(
        request.POST or None, instance=bill, prefix="lines", initial=line_initial
    )
    if not request.POST and creating:
        vendor_id = request.GET.get("vendor") or (wo.vendor_id if wo else None)
        if vendor_id:
            form.initial["vendor"] = vendor_id
        if wo is not None:
            form.initial["description"] = str(wo)[:255]
        if proposal:
            form.initial.update({k: v for k, v in proposal["fields"].items() if v})
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            if creating:
                obj.created_by = request.user
            obj.save()
            formset.instance = obj
            for line in formset.save(commit=False):
                line.association = obj.association
                line.save()
            for line in formset.deleted_objects:
                line.delete()
            upload = form.cleaned_data.get("attachment")
            if upload:
                services.add_attachment(obj, upload, request.user)
                if held_data:
                    prefill.discard_held(held, request.association)
            elif held_data:
                prefill.attach_held(obj, held, request.user)
            if wo is not None:
                _link_work_order(request, wo, obj)
        if "submit" in request.POST:
            try:
                services.submit(obj, request.user)
            except services.BillError as exc:
                messages.error(request, str(exc))
                return redirect("ap:edit", pk=obj.pk)
            messages.success(request, _("Bill submitted and booked to accounts payable."))
        else:
            messages.success(request, _("Draft saved."))
        return redirect("ap:detail", pk=obj.pk)
    title = _("Add bill") if creating else _("Edit bill %(bill)s") % {"bill": bill}
    status = 400 if request.method == "POST" else 200
    return _render(
        request,
        "ap/form.html",
        {
            "form": form,
            "formset": formset,
            "title": title,
            "bill": bill,
            "work_order": wo,
            "proposal": proposal,
            "held_file": held if held_data else "",
            "held_name": held_data["name"] if held_data else "",
            "invoice_reader": creating and _invoice_reader_on(request),
        },
        status=status,
    )


@require_perm(*ENTER)
def add(request):
    return _edit(request, Bill())


@require_perm(*ENTER)
def edit(request, pk):
    bill = _bill(pk)
    if not bill.is_editable:
        messages.error(request, _("A submitted bill cannot be edited. Void it and enter it again."))
        return redirect("ap:detail", pk=bill.pk)
    return _edit(request, bill)


@require_POST
@require_perm(*ENTER)
def submit(request, pk):
    bill = _bill(pk)
    try:
        services.submit(bill, request.user)
    except services.BillError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Bill submitted and booked to accounts payable."))
    return redirect("ap:detail", pk=bill.pk)


def _describe_bill(verb):
    def describe(request, pk):
        bill = Bill.objects.select_related("vendor").filter(pk=pk).first()
        label = f"{bill.vendor.name} ${bill.total:,.2f}" if bill else f"#{pk}"
        details = [(_("Bill"), label)]
        if request.POST.get("reason"):
            details.append((_("Reason"), request.POST["reason"]))
        if request.POST.get("paid_on"):
            details.append((_("Paid on"), date_value(request.POST["paid_on"])))
        return str(verb) % {"bill": label}, details

    return describe


@money_change(
    "money.bill_void", _l("Void a bill"), describe=_describe_bill(_l("Void bill %(bill)s"))
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def void(request, pk):
    bill = _bill(pk)
    form = VoidForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Give a reason for voiding the bill."))
        return redirect("ap:detail", pk=bill.pk)
    try:
        services.void(bill, request.user, form.cleaned_data["reason"])
    except services.BillError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Bill voided."))
    return redirect("ap:detail", pk=bill.pk)


@money_change(
    "money.bill_paid",
    _l("Mark a bill paid"),
    describe=_describe_bill(_l("Mark bill %(bill)s paid")),
)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def mark_paid(request, pk):
    bill = _bill(pk)
    form = MarkPaidForm(request.POST, funds=list(services.fund_totals(bill)))
    if not form.is_valid():
        messages.error(request, _("Pick the payment date and bank account."))
        return redirect("ap:detail", pk=bill.pk)
    try:
        services.mark_paid(
            bill,
            request.user,
            paid_on=form.cleaned_data["paid_on"],
            bank_accounts=form.bank_accounts(),
            paid_by=form.cleaned_data["paid_by"] or Bill.PaidBy.BANK,
        )
    except services.BillError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Bill marked paid."))
    return redirect("ap:detail", pk=bill.pk)


@require_POST
@require_perm(*ENTER)
def attach(request, pk):
    bill = _bill(pk)
    form = AttachmentForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors.get("attachment", [])))
        return redirect("ap:detail", pk=bill.pk)
    services.add_attachment(bill, form.cleaned_data["attachment"], request.user)
    messages.success(request, _("File attached."))
    return redirect("ap:detail", pk=bill.pk)


@require_perm(*READ)
def attachment(request, pk, att):
    obj = get_object_or_404(BillAttachment, pk=att, bill_id=pk)
    try:
        content = services.attachment_content(obj)
    except LookupError:
        raise Http404 from None
    resp = HttpResponse(content, content_type=obj.content_type)
    safe = obj.name.replace('"', "")
    resp["Content-Disposition"] = f'inline; filename="{safe}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    )
    resp["X-Frame-Options"] = "SAMEORIGIN"
    return resp


# --- approvals ------------------------------------------------------------------


def _approval_context(request, bill) -> dict:
    done, required = approval_services.progress(bill)
    history = Approval.objects.filter(bill=bill).select_related("user")
    return {
        "bill": bill,
        "approvals_done": done,
        "approvals_required": required,
        "can_approve": approval_services.can_approve(bill, request.user),
        "waiting_on": approval_services.waiting_on(bill),
        "approval_history": history,
        "reject_form": RejectForm(),
    }


def _after_decision(request, bill):
    if request.htmx:
        bill = _bill(bill.pk)
        _decorate([bill], association_today(request.association))
        return render(request, "ap/_approval.html", _approval_context(request, bill))
    return redirect("ap:detail", pk=bill.pk)


@require_POST
@require_perm(perms.BILLS_APPROVE)
def approve(request, pk):
    bill = _bill(pk)
    try:
        bill = approval_services.approve(bill, request.user)
    except approval_services.ApprovalError as exc:
        messages.error(request, str(exc))
    else:
        if bill.status == Bill.Status.APPROVED:
            messages.success(request, _("Bill approved. Pay it at the bank."))
        else:
            messages.success(request, _("Approval recorded."))
    return _after_decision(request, bill)


@require_POST
@require_perm(perms.BILLS_APPROVE)
def reject(request, pk):
    bill = _bill(pk)
    form = RejectForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Give a reason for rejecting the bill."))
        return _after_decision(request, bill)
    try:
        bill = approval_services.reject(bill, request.user, form.cleaned_data["reason"])
    except approval_services.ApprovalError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Bill rejected and returned to draft."))
    return _after_decision(request, bill)


# --- approval rules settings page -----------------------------------------------


@require_perm(perms.POLICY_PROPOSE)
def rules(request):
    from homeoa import rules as rule_values

    rule_list = ApprovalRule.objects.select_related("vendor").order_by("priority", "pk")
    reserve = rule_values.resolve(request.association, "reserves.spend_min_approvals")
    return _render(request, "ap/rules.html", {"rules": rule_list, "reserve_rule": reserve})


def _rule_edit(request, rule):
    form = ApprovalRuleForm(request.POST or None, instance=rule, association=request.association)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Approval rule saved."))
        return redirect("ap:rules")
    title = _("Add approval rule") if rule.pk is None else _("Edit approval rule")
    status = 400 if request.method == "POST" else 200
    return _render(
        request, "ap/rule_form.html", {"form": form, "title": title, "rule": rule}, status
    )


@change_view(
    "policy.approval_rule_add",
    _l("Add a bill approval rule"),
    manage=perms.POLICY_PROPOSE,
    propose=perms.POLICY_PROPOSE,
    codes={perms.POLICY_PROPOSE},
    always=True,
)
@require_perm(perms.POLICY_PROPOSE)
def rule_add(request):
    return _rule_edit(request, ApprovalRule())


@change_view(
    "policy.approval_rule_edit",
    _l("Change a bill approval rule"),
    manage=perms.POLICY_PROPOSE,
    propose=perms.POLICY_PROPOSE,
    codes={perms.POLICY_PROPOSE},
    always=True,
)
@require_perm(perms.POLICY_PROPOSE)
def rule_edit(request, pk):
    return _rule_edit(request, get_object_or_404(ApprovalRule, pk=pk))
