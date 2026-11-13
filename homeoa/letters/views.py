"""Board UI for letter templates: /settings/letters/."""

from django.contrib import messages
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.providers import get_provider
from homeoa.registry.models import Unit

from . import batches as mail
from . import merge, services
from . import render as letter_render
from .forms import BatchForm, GenerateForm, LetterheadForm, TemplateForm, TranslationForm
from .letterhead import letterhead, set_letterhead
from .models import Letter, LetterTemplate, LetterTranslation, MailBatch

PERM = perms.LETTERS_MANAGE


def _base(request, **extra):
    return {"current_hub": None, "subtabs": [], **extra}


@require_perm(PERM)
def index(request):
    services.ensure_default_templates(request.association)
    templates = LetterTemplate.objects.select_related("current_version")
    head = letterhead(request.association)
    form = LetterheadForm(
        initial={
            "address": "\n".join(head["address_lines"]),
            "phone": head["phone"],
            "email": head["email"],
        }
    )
    return render(
        request,
        "letters/index.html",
        _base(request, templates=templates, letterhead_form=form, head=head),
    )


@require_perm(PERM)
@require_POST
def letterhead_edit(request):
    form = LetterheadForm(request.POST)
    if form.is_valid():
        set_letterhead(
            request.association,
            address_lines=form.cleaned_data["address"].splitlines(),
            phone=form.cleaned_data["phone"],
            email=form.cleaned_data["email"],
        )
        messages.success(request, _("Letterhead saved."))
    else:
        messages.error(request, _("Check the letterhead fields and try again."))
    return redirect("letters:index")


def _units():
    return Unit.objects.order_by("number")


def _editor(request, form, template=None, status=200, **extra):
    ctx = _base(
        request,
        **extra,
        form=form,
        template=template,
        units=_units(),
        fields=merge.fields(request.association).values(),
    )
    if template is not None:
        ctx["versions"] = template.versions.all()[:20]
        ctx["letters"] = Letter.objects.filter(template_version__template=template).select_related(
            "unit", "template_version"
        )[:20]
        ctx["generate_form"] = GenerateForm(
            initial={"as_of": association_today(request.association)}
        )
        ctx["translations"] = template.translations.select_related("uploaded_by")[:50]
        ctx["translation_form"] = ctx.get("translation_form") or TranslationForm(
            initial={"language": request.GET.get("language") or None}
        )
    return render(request, "letters/edit.html", ctx, status=status)


@require_perm(PERM)
def new(request):
    if request.method == "POST":
        form = TemplateForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            template = services.create_template(
                request.association,
                name=d["name"],
                kind=d["kind"],
                subject=d["subject"],
                body=d["body"],
                draft_wording=d["draft_wording"],
                user=request.user,
            )
            messages.success(request, _("Template created."))
            return redirect("letters:edit", pk=template.pk)
        return _editor(request, form, status=400)
    return _editor(request, TemplateForm(initial={"kind": LetterTemplate.Kind.GENERAL}))


@require_perm(PERM)
def edit(request, pk):
    template = get_object_or_404(LetterTemplate, pk=pk)
    version = template.current_version
    if request.method == "POST":
        form = TemplateForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            template.name = d["name"]
            template.kind = d["kind"]
            template.draft_wording = d["draft_wording"]
            template.save()
            if version is None or (d["subject"], d["body"]) != (version.subject, version.body):
                services.save_version(
                    template,
                    subject=d["subject"],
                    body=d["body"],
                    user=request.user,
                    note=d["note"],
                )
            messages.success(request, _("Template saved."))
            return redirect("letters:edit", pk=template.pk)
        return _editor(request, form, template, status=400)
    form = TemplateForm(
        initial={
            "name": template.name,
            "kind": template.kind,
            "draft_wording": template.draft_wording,
            "subject": version.subject if version else "",
            "body": version.body if version else "",
        }
    )
    return _editor(request, form, template)


def _unit_from(request, key="unit"):
    raw = request.POST.get(key) or request.GET.get(key) or ""
    if not raw:
        return None
    try:
        return Unit.objects.get(pk=int(raw))
    except ValueError, Unit.DoesNotExist:
        raise Http404 from None


@require_perm(PERM)
@require_POST
def preview(request):
    """htmx partial: the letter rendered against a unit (or sample values)."""
    unit = _unit_from(request)
    subject = request.POST.get("subject", "")
    body = request.POST.get("body", "")
    ctx = services.context_for(request.association, unit) if unit else None
    try:
        r_subject, r_body = services.preview(subject, body, ctx)
        result = {"subject": r_subject, "body": r_body, "errors": []}
    except letter_render.TemplateRejected as exc:
        result = {"errors": exc.result.errors}
    if unit is not None:
        result["recipient"] = services.recipient(ctx)[1]
    return render(request, "letters/_preview.html", {"result": result, "unit": unit})


@require_perm(PERM)
def preview_pdf(request, pk):
    template = get_object_or_404(LetterTemplate, pk=pk)
    unit = _unit_from(request)
    if template.current_version is None or unit is None:
        raise Http404
    ctx = services.context_for(request.association, unit)
    data, _meta = services.render_pdf(template.current_version, ctx)
    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="preview-{template.key}.pdf"'
    return resp


@require_perm(PERM)
@require_POST
def generate(request, pk):
    template = get_object_or_404(LetterTemplate, pk=pk)
    form = GenerateForm(request.POST)
    if template.current_version is None or not form.is_valid():
        messages.error(request, _("Choose a unit and a letter date."))
        return redirect("letters:edit", pk=pk)
    letter = services.generate_letter(
        template,
        form.cleaned_data["unit"],
        as_of=form.cleaned_data["as_of"],
        user=request.user,
    )
    messages.success(
        request,
        _("Letter for unit %(unit)s generated and saved.") % {"unit": letter.unit.number},
    )
    return redirect("letters:edit", pk=pk)


@require_perm(PERM)
def download(request, pk):
    letter = get_object_or_404(Letter, pk=pk)
    resp = HttpResponse(services.letter_bytes(letter), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="letter-{letter.pk}.pdf"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp


# --- mail batches (COM-04) ----------------------------------------------------------------


def _unbatched_letters():
    return (
        Letter.objects.exclude(pk__in=mail.active_items(Letter.objects.all()).values("letter"))
        .exclude(mail_items__batch__status=MailBatch.Status.DRAFT)
        .select_related("unit", "template_version__template")
        .order_by("-generated_at", "-pk")
    )


@require_perm(PERM)
def batches(request):
    form = BatchForm(request.POST or None, letters=_unbatched_letters())
    if request.method == "POST":
        if form.is_valid():
            d = form.cleaned_data
            batch = mail.create_batch(
                request.association,
                list(d["letters"]),
                name=d["name"],
                certified=d["certified"],
                user=request.user,
            )
            messages.success(request, _("Mail batch created as a draft."))
            return redirect("letters:batch", pk=batch.pk)
        status = 400
    else:
        status = 200
    provider = get_provider("PostalProvider", request.association)
    return render(
        request,
        "letters/batches.html",
        _base(
            request,
            batches=MailBatch.objects.annotate(n=Count("items")),
            form=form,
            provider=provider,
            residents_on=residents_enabled(request.association),
        ),
        status=status,
    )


@require_perm(PERM)
def batch_detail(request, pk):
    batch = get_object_or_404(MailBatch, pk=pk)
    items = batch.items.select_related("letter__unit", "letter__template_version__template")
    return render(
        request,
        "letters/batch.html",
        _base(
            request,
            batch=batch,
            items=items,
            provider=get_provider("PostalProvider", request.association),
            residents_on=residents_enabled(request.association),
        ),
    )


@require_perm(PERM)
@require_POST
def batch_send(request, pk):
    batch = get_object_or_404(MailBatch, pk=pk)
    try:
        batch = mail.send_batch(batch, user=request.user)
    except mail.BatchError as exc:
        messages.error(request, str(exc))
    else:
        if batch.status == MailBatch.Status.READY:
            messages.success(request, _("Batch ready to print. Mail it, then mark it mailed here."))
        else:
            messages.success(request, _("Batch sent to the mail service."))
    return redirect("letters:batch", pk=pk)


@require_perm(PERM)
@require_POST
def batch_mailed(request, pk):
    batch = get_object_or_404(MailBatch, pk=pk)
    try:
        mail.confirm_mailed(batch, user=request.user)
    except mail.BatchError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Batch marked as printed and mailed by hand."))
    return redirect("letters:batch", pk=pk)


@require_perm(PERM)
def batch_file(request, pk, which):
    batch = get_object_or_404(MailBatch, pk=pk)
    if which not in ("pdf", "csv"):
        raise Http404
    key = batch.pdf_key if which == "pdf" else batch.manifest_key
    if not key:
        raise Http404
    ctype = "application/pdf" if which == "pdf" else "text/csv"
    resp = HttpResponse(mail.file_bytes(batch, which), content_type=ctype)
    disposition = "inline" if which == "pdf" else "attachment"
    resp["Content-Disposition"] = f'{disposition}; filename="mail-batch-{batch.pk}.{which}"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp


@require_perm(PERM)
@require_POST
def translation_upload(request, pk):
    template = get_object_or_404(LetterTemplate, pk=pk)
    form = TranslationForm(request.POST, request.FILES)
    if form.is_valid():
        d = form.cleaned_data
        upload = d.get("pdf")
        try:
            services.upload_translation(
                template,
                d["language"],
                text=d.get("text") or "",
                pdf_bytes=upload.read() if upload else None,
                filename=upload.name if upload else "",
                note=d.get("note") or "",
                user=request.user,
            )
        except services.TranslationError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Translation saved. Held notices go out on the next run."))
            return redirect("letters:edit", pk=template.pk)
    version = template.current_version
    edit_form = TemplateForm(
        initial={
            "name": template.name,
            "kind": template.kind,
            "draft_wording": template.draft_wording,
            "subject": version.subject if version else "",
            "body": version.body if version else "",
        }
    )
    return _editor(request, edit_form, template, status=400, translation_form=form)


@require_perm(PERM)
def translation_file(request, pk):
    translation = get_object_or_404(LetterTranslation, pk=pk)
    if not translation.is_pdf:
        raise Http404
    response = HttpResponse(services.translation_bytes(translation), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{translation.filename}"'
    return response
