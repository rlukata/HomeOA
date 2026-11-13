"""Vendors hub: All vendors list, vendor page, add/edit, contacts, W-9, TIN reveal.

Reads need ``vendors.read`` (or ``vendors.manage``), writes ``vendors.manage``; revealing
the TIN needs ``finance.manage`` and is audited.
"""

from __future__ import annotations

import mimetypes

from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.changes.models import ChangeRequest
from homeoa.core.context import association_today
from homeoa.core.fields import ZERO
from homeoa.ui.nav import get_hub

from . import alerts, services, vendor_changes
from .forms import ContactForm, ContractForm, VendorDocumentForm, VendorForm
from .models import Contract, Vendor, VendorAlert, VendorContact, VendorDocument

READ = (perms.VENDORS_READ, perms.VENDORS_MANAGE)
EDIT = (perms.VENDORS_MANAGE, perms.VENDORS_PROPOSE)
DOC_FOLDER = "vendors/certificates"
CONTRACT_FOLDER = "vendors/contracts"


def _render(request, template, context, tab="all"):
    hub = get_hub("vendors")
    slugs = [t.slug for t in hub.subtabs]
    index = slugs.index(tab) if tab in slugs else -1
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": i == index}
        for i, t in enumerate(hub.subtabs)
    ]
    user = request.user
    assoc = request.association
    return render(
        request,
        template,
        {
            "current_hub": hub,
            "subtabs": tabs,
            "can_manage": has_perm(user, perms.VENDORS_MANAGE, association=assoc),
            "can_propose": has_perm(user, perms.VENDORS_PROPOSE, association=assoc),
            "can_reveal": has_perm(user, perms.FINANCE_MANAGE, association=assoc),
            **context,
        },
    )


def _vendor(slug) -> Vendor:
    return get_object_or_404(
        Vendor.objects.select_related("default_expense_account").prefetch_related("contacts"),
        slug=slug,
    )


@require_perm(*READ)
def vendor_list(request):
    query = request.GET.get("q", "")
    status = request.GET.get("status", "active")
    today = association_today()
    qs = Vendor.objects.prefetch_related(
        "contacts",
        Prefetch(
            "contracts",
            queryset=Contract.objects.order_by("-end_date", "-pk"),
            to_attr="contracts_by_end",
        ),
        Prefetch(
            "documents",
            queryset=VendorDocument.objects.filter(kind=VendorDocument.Kind.COI).order_by(
                "-expiry_date", "-pk"
            ),
            to_attr="cois_by_expiry",
        ),
        Prefetch(
            "documents",
            queryset=VendorDocument.objects.filter(kind=VendorDocument.Kind.LICENSE).order_by(
                "-expiry_date", "-pk"
            ),
            to_attr="licenses_by_expiry",
        ),
    )
    vendors = list(services.search(qs, query, "" if status == "all" else status))
    paid = services.paid_by_vendor(request.association, today)
    for v in vendors:
        v.contract = next(iter(v.contracts_by_end), None)
        v.contract_state = alerts.contract_state(v.contract, today) if v.contract else ""
        v.coi = next(iter(v.cois_by_expiry), None)
        v.coi_state = alerts.document_state(v.coi.expiry_date, today) if v.coi else ""
        lic = next(iter(v.licenses_by_expiry), None)
        v.license_expired = bool(lic) and alerts.document_state(lic.expiry_date, today) == "danger"
        v.paid_this_year = paid.get(v.pk, ZERO)
    context = {
        "vendors": vendors,
        "q": query,
        "status": status,
        "statuses": [("active", _("Active")), ("inactive", _("Inactive")), ("all", _("All"))],
    }
    template = "vendors/_rows.html" if request.htmx else "vendors/list.html"
    return _render(request, template, context)


@require_perm(*READ)
def vendor_detail(request, slug):
    vendor = _vendor(slug)
    today = association_today()
    documents = list(vendor.documents.prefetch_related("requests"))
    current = {
        d.pk for d in alerts.current_documents(request.association) if d.vendor_id == vendor.pk
    }
    for d in documents:
        d.current = d.pk in current
        d.expired = d.expiry_date < today
        d.last_request = next(iter(d.requests.all()), None)
    contracts = list(vendor.contracts.all())
    open_alerts = VendorAlert.objects.filter(resolved_on__isnull=True, contract__vendor=vendor)
    alert_by_contract = {a.contract_id: a for a in open_alerts}
    for c in contracts:
        c.open_alert = alert_by_contract.get(c.pk)
    context = {
        "vendor": vendor,
        "documents": documents,
        "contracts": contracts,
        "today": today,
    }
    return _render(request, "vendors/detail.html", context)


def _save_form(request, form):
    with transaction.atomic():
        vendor = services.save_vendor(form.save(commit=False))
        upload = form.cleaned_data.get("w9_file")
        if upload:
            services.store_w9(vendor, upload)
    return vendor


def _proposed(request, change, done_message, fallback):
    """Applied at once (nobody else on the board) or sent for board approval."""
    if change.status == ChangeRequest.Status.APPLIED:
        messages.success(request, done_message)
        return redirect(change.result_url or fallback)
    if change.status == ChangeRequest.Status.FAILED:
        messages.error(request, change.error)
        return redirect(fallback)
    messages.success(
        request,
        _("Sent for board approval. It takes effect once approved (%(n)s needed).")
        % {"n": change.required_approvals},
    )
    return redirect("changes:detail", pk=change.pk)


@require_perm(perms.VENDORS_PROPOSE)
def vendor_add(request):
    """A new vendor is always a change request."""
    form = VendorForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        change = vendor_changes.propose_add(request.association, form, user=request.user)
        return _proposed(request, change, _("Vendor added."), reverse("vendors:index"))
    return _render(
        request,
        "vendors/form.html",
        {"form": form, "title": _("Add vendor"), "needs_approval": True},
    )


@require_perm(*EDIT)
def vendor_edit(request, slug):
    """Name, category, expense account and notes save directly for board members; payment,
    tax (W-9) and active-status changes go to the board as a change request."""
    vendor = _vendor(slug)
    form = VendorForm(request.POST or None, request.FILES or None, instance=vendor)
    direct = has_perm(request.user, perms.VENDORS_MANAGE, association=request.association)
    if request.method == "POST" and form.is_valid():
        # Validation copied the posted values onto ``vendor``; compare with what is stored.
        stored = Vendor.objects.get(pk=vendor.pk)
        if direct and not vendor_changes.needs_approval(form, stored):
            _save_form(request, form)
            messages.success(request, _("Vendor saved."))
            return redirect("vendors:detail", slug=vendor.slug)
        change = vendor_changes.propose_edit(request.association, stored, form, user=request.user)
        return _proposed(
            request, change, _("Vendor saved."), reverse("vendors:detail", args=[vendor.slug])
        )
    return _render(
        request,
        "vendors/form.html",
        {
            "form": form,
            "title": _("Edit %(name)s") % {"name": vendor.name},
            "vendor": vendor,
            "needs_approval": True,
        },
    )


@require_POST
@require_perm(perms.FINANCE_MANAGE)
def reveal_tin(request, slug):
    vendor = _vendor(slug)
    clear = services.reveal_tin(vendor, request.user)
    response = render(request, "vendors/_tin.html", {"vendor": vendor, "clear": clear})
    response["Cache-Control"] = "no-store"
    return response


@require_perm(perms.FINANCE_MANAGE)
def w9_download(request, slug):
    """The W-9 shows the TIN in clear, so it needs ``finance.manage`` and is audited."""
    vendor = _vendor(slug)
    if not vendor.w9_file_key:
        raise Http404
    audit.record("vendors.w9_downloaded", vendor, actor=request.user)
    resp = HttpResponse(services.w9_content(vendor), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="w9-{vendor.slug}.pdf"'
    resp["Referrer-Policy"] = "no-referrer"
    resp["Cache-Control"] = "no-store"
    return resp


@require_perm(perms.VENDORS_MANAGE)
def contact_edit(request, slug, pk=None):
    vendor = _vendor(slug)
    contact = get_object_or_404(VendorContact, pk=pk, vendor=vendor) if pk else None
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.vendor = vendor
            if obj.is_primary:
                for other in vendor.contacts.exclude(pk=obj.pk).filter(is_primary=True):
                    other.is_primary = False
                    other.save()
            obj.save()
        messages.success(request, _("Contact saved."))
        return redirect("vendors:detail", slug=vendor.slug)
    title = _("Edit contact") if contact else _("Add contact")
    return _render(request, "vendors/form.html", {"form": form, "title": title, "vendor": vendor})


@require_perm(perms.VENDORS_MANAGE)
def document_add(request, slug):
    vendor = _vendor(slug)
    form = VendorDocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            doc = form.save(commit=False)
            doc.vendor = vendor
            doc.save()
            if form.cleaned_data.get("upload"):
                services.store_file(doc, form.cleaned_data["upload"], DOC_FOLDER)
            # A renewed certificate closes the old one's alert today, not at the next run.
            alerts.run(request.association, association_today(request.association))
        messages.success(request, _("Certificate saved."))
        return redirect("vendors:detail", slug=vendor.slug)
    return _render(
        request,
        "vendors/form.html",
        {"form": form, "title": _("Add certificate or license"), "vendor": vendor},
    )


@require_perm(perms.VENDORS_MANAGE)
def contract_edit(request, slug, pk=None):
    vendor = _vendor(slug)
    contract = get_object_or_404(Contract, pk=pk, vendor=vendor) if pk else None
    form = ContractForm(request.POST or None, request.FILES or None, instance=contract)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.vendor = vendor
            obj.save()
            if form.cleaned_data.get("upload"):
                services.store_file(obj, form.cleaned_data["upload"], CONTRACT_FOLDER)
        messages.success(request, _("Contract saved."))
        return redirect("vendors:detail", slug=vendor.slug)
    title = _("Edit contract") if contract else _("Add contract")
    return _render(request, "vendors/form.html", {"form": form, "title": title, "vendor": vendor})


def _file_response(obj, kind):
    if not obj.file_key:
        raise Http404
    resp = HttpResponse(
        services.file_content(obj),
        content_type=mimetypes.guess_type(obj.file_name)[0] or "application/octet-stream",
    )
    resp["Content-Disposition"] = f'inline; filename="{kind}-{obj.pk}"'
    resp["Cache-Control"] = "private"
    return resp


@require_perm(*READ)
def document_file(request, pk):
    return _file_response(get_object_or_404(VendorDocument, pk=pk), "certificate")


@require_perm(*READ)
def contract_file(request, pk):
    return _file_response(get_object_or_404(Contract, pk=pk), "contract")


@require_POST
@require_perm(perms.VENDORS_MANAGE)
def document_request(request, pk):
    doc = get_object_or_404(VendorDocument.objects.select_related("vendor"), pk=pk)
    try:
        req = alerts.send_document_request(doc, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        if req.sent:
            messages.success(request, _("Request sent to %(email)s.") % {"email": req.email})
        else:
            messages.error(request, _("The request email could not be sent."))
    return redirect("vendors:detail", slug=doc.vendor.slug)


@require_POST
@require_perm(perms.VENDORS_MANAGE)
def alert_resolve(request, pk):
    alert = get_object_or_404(VendorAlert, pk=pk)
    alerts.resolve(alert, association_today(request.association), request.user)
    messages.success(request, _("Marked done."))
    target = alert.document or alert.contract
    return redirect("vendors:detail", slug=target.vendor.slug)


@require_POST
@require_perm(perms.VENDORS_MANAGE)
def contact_delete(request, slug, pk):
    vendor = _vendor(slug)
    get_object_or_404(VendorContact, pk=pk, vendor=vendor).delete()
    messages.success(request, _("Contact removed."))
    return redirect("vendors:detail", slug=vendor.slug)


@require_perm(*READ)
def ten99_view(request):
    """Vendors > 1099 tracking: cash paid per 1099-eligible vendor in a calendar year."""
    from homeoa.ui.export import export_format, export_response

    from . import ten99

    this_year = association_today(request.association).year
    try:
        year = int(request.GET.get("year", this_year))
    except ValueError:
        year = this_year
    totals = ten99.totals(request.association, year)
    if fmt := export_format(request):
        return export_response(request, ten99.table(totals), fmt)
    context = {
        "totals": totals,
        "year": year,
        "years": list(range(this_year, this_year - 6, -1)),
        "threshold": ten99.threshold(),
    }
    return _render(request, "vendors/ten99.html", context, tab="1099")
