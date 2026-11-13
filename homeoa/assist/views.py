from django import forms
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.ap import prefill
from homeoa.ap.forms import check_upload
from homeoa.providers import get_provider

from . import invoices

READABLE = ("application/pdf", "image/png", "image/jpeg")


@require_POST
@require_perm(perms.FINANCE_MANAGE, perms.BILLS_ENTER)
def bill_from_invoice(request):
    """Money > Bills > Add bill > Fill in from invoice: AI reads the invoice, and the Add
    bill form opens filled in for the person to check. Nothing is saved here."""
    association = request.association
    provider = get_provider("AiProvider", association)
    if not provider.available:
        raise Http404
    upload = request.FILES.get("invoice")
    try:
        upload = check_upload(upload)
    except forms.ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("ap:add")
    content_type = getattr(upload, "content_type", "") if upload else ""
    if upload is None or content_type not in READABLE:
        messages.error(request, _("Choose a PDF, PNG or JPEG invoice."))
        return redirect("ap:add")
    content = upload.read()
    try:
        proposal = invoices.read(association, content, content_type, upload.name)
    except invoices.CouldNotRead:
        messages.error(
            request, _("The invoice could not be read. Please fill in the bill by hand.")
        )
        return redirect("ap:add")
    source = _("AI (%(provider)s, %(model)s) from %(file)s") % {
        "provider": provider.name,
        "model": getattr(provider, "model", ""),
        "file": upload.name,
    }
    proposal["file"] = prefill.hold_file(association, upload.name, content, content_type, source)
    key = prefill.stash(request, proposal)
    return redirect(f"{reverse('ap:add')}?prefill={key}")
