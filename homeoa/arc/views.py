"""Board pages for architectural review (Requests > Architectural)."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.registry.models import Unit
from homeoa.registry.services import current_owners

from . import deadlines, deemed, services
from .models import ArcForm, ArcRequest, FieldKind, StatutoryKind, VoteChoice

REVIEW = (perms.ARCH_REVIEW, perms.ARCH_DECIDE)


def _can_decide(request) -> bool:
    return has_perm(request.user, perms.ARCH_DECIDE, association=request.association)


def _decide_only(request) -> None:
    if not _can_decide(request):
        raise PermissionDenied


def files_from(request) -> list[tuple[str, bytes]]:
    from homeoa.documents import services as docs

    out = []
    for f in request.FILES.getlist("files"):
        docs.check_upload(f.name, f.size)
        out.append((f.name, f.read()))
    return out


def file_response(att) -> HttpResponse:
    from homeoa.documents import services as docs

    version = att.version
    resp = HttpResponse(docs.read_file(version), content_type=version.content_type)
    safe = version.file_name.replace('"', "")
    resp["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store"
    return resp


def field_rows(form, values, errors) -> list[dict]:
    """The form's fields with the posted value and error, for ``arc/_fields.html``."""
    if form is None:
        return []
    return [
        {**f, "value": values.get(f["key"], ""), "error": errors.get(f["key"], "")}
        for f in form.fields
    ]


def form_choice(request) -> ArcForm | None:
    forms = ArcForm.objects.filter(active=True)
    pk = request.POST.get("form") or request.GET.get("form")
    if pk and str(pk).isdigit():
        return forms.filter(pk=pk).first()
    return forms.first()


@require_perm(*REVIEW)
def index(request):
    services.ensure_default_form(request.association)
    reqs = ArcRequest.objects.select_related("unit", "submitted_by").prefetch_related("votes")
    open_reqs = [r for r in reqs if r.is_open]
    of = len(services.reviewers(request.association))
    for r in open_reqs:
        r.votes_in = len(r.votes.all())
        r.votes_of = of
    return render(
        request,
        "arc/index.html",
        {
            "open_reqs": open_reqs,
            "closed_reqs": [r for r in reqs if not r.is_open],
            "can_decide": _can_decide(request),
            "committee": services.reviewer_committee(request.association),
        },
    )


@require_perm(perms.ARCH_DECIDE)
def board_new(request):
    """Submit on behalf of a unit."""
    a = request.association
    services.ensure_default_form(a)
    form = form_choice(request)
    units = list(Unit.objects.all())
    unit = None
    unit_pk = request.POST.get("unit") or request.GET.get("unit")
    if unit_pk and str(unit_pk).isdigit():
        unit = Unit.objects.filter(pk=unit_pk).first()
    owners = current_owners(unit) if unit else []
    errors, values = {}, {}
    if request.method == "POST" and request.POST.get("submit") and form and unit:
        values = request.POST
        person = next((p for p in owners if str(p.pk) == request.POST.get("person")), None)
        if person is None:
            messages.error(request, _("Choose the owner the request is for."))
        else:
            try:
                req = services.submit(
                    a,
                    unit=unit,
                    person=person,
                    form=form,
                    title=request.POST.get("title", ""),
                    data=request.POST,
                    files=files_from(request),
                    user=request.user,
                )
                messages.success(request, _("Request submitted for %(unit)s.") % {"unit": unit})
                return redirect("arc:detail", req.pk)
            except services.ArcError as exc:
                errors = getattr(exc, "errors", {})
                messages.error(request, str(exc))
            except ValidationError as exc:  # upload rejected
                messages.error(request, " ".join(exc.messages))
    return render(
        request,
        "arc/new.html",
        {
            "forms": ArcForm.objects.filter(active=True),
            "form": form,
            "units": units,
            "unit": unit,
            "owners": owners,
            "rows": field_rows(form, values, errors),
            "title_error": errors.get("title", ""),
            "values": values,
            "action_url": request.path,
        },
    )


@require_perm(*REVIEW)
def detail(request, pk):
    a = request.association
    req = get_object_or_404(
        ArcRequest.objects.select_related("unit", "submitted_by", "form"), pk=pk
    )
    me = services.reviewer_for(request.user, a)
    my_vote = req.votes.filter(reviewer=me).first() if me else None
    return render(
        request,
        "arc/detail.html",
        {
            "req": req,
            "tally": services.tally(req, a),
            "comments": req.comments.all(),
            "attachments": req.attachments.select_related("version"),
            "me": me,
            "my_vote": my_vote,
            "vote_choices": VoteChoice.choices,
            "decisions": [c for c in ArcRequest.Status.choices if c[0] in ArcRequest.DECISIONS],
            "can_decide": _can_decide(request),
            "overdue": req.is_overdue(association_today(a)),
            "deemed_approved": deadlines.deemed_approved(a),
            "statutory_kinds": StatutoryKind.choices,
        },
    )


@require_POST
@require_perm(*REVIEW)
def act(request, pk):
    a = request.association
    req = get_object_or_404(ArcRequest.objects, pk=pk)
    action = request.POST.get("action", "")
    try:
        if action == "comment":
            services.add_comment(
                req,
                body=request.POST.get("body"),
                user=request.user,
                internal=request.POST.get("visible") != "on",
            )
            messages.success(request, _("Comment added."))
        elif action == "vote":
            me = services.reviewer_for(request.user, a)
            if me is None:
                raise services.ArcError(_("Only current reviewers can vote."))
            services.cast_vote(
                req, reviewer=me, vote=request.POST.get("vote", ""), note=request.POST.get("note")
            )
            messages.success(request, _("Vote recorded."))
        elif action == "request_info":
            _decide_only(request)
            services.request_info(req, message=request.POST.get("body"), user=request.user)
            messages.success(request, _("Information requested from the owner."))
        elif action == "decide":
            _decide_only(request)
            services.decide(
                req,
                outcome=request.POST.get("outcome", ""),
                conditions=request.POST.get("conditions", ""),
                note=request.POST.get("note", ""),
                citations=request.POST.get("citations", ""),
                user=request.user,
            )
            messages.success(
                request, _("Decision recorded. The decision letter is being prepared.")
            )
        elif action == "statutory_kind":
            _decide_only(request)
            if not req.is_open:
                raise services.ArcError(_("This request is closed."))
            kind = request.POST.get("statutory_kind", "")
            if kind not in StatutoryKind.values:
                raise services.ArcError(_("Choose one of the options."))
            deemed.set_kind(req, kind, user=request.user)
            messages.success(request, _("Deadline updated."))
        elif action == "withdraw":
            _decide_only(request)
            services.withdraw(req, user=request.user)
            messages.success(request, _("Request withdrawn."))
        else:
            messages.error(request, _("Unknown action."))
    except services.ArcError as exc:
        messages.error(request, str(exc))
    return redirect("arc:detail", req.pk)


@require_perm(*REVIEW)
def file(request, pk, att):
    req = get_object_or_404(ArcRequest.objects, pk=pk)
    return file_response(get_object_or_404(req.attachments.select_related("version"), pk=att))


# ------------------------------------------------------------------ form builder


@require_perm(*REVIEW)
def forms_index(request):
    from homeoa.governance.models import Committee

    services.ensure_default_form(request.association)
    return render(
        request,
        "arc/forms.html",
        {
            "forms": ArcForm.objects.all(),
            "committees": Committee.objects.filter(active=True),
            "committee": services.reviewer_committee(request.association),
            "can_decide": _can_decide(request),
        },
    )


EXTRA_ROWS = 3


@require_perm(perms.ARCH_DECIDE)
def form_edit(request, pk=None):
    form = get_object_or_404(ArcForm.objects, pk=pk) if pk else None
    rows = list(form.fields) if form else []
    if request.method == "POST":
        labels = request.POST.getlist("label")
        kinds = request.POST.getlist("kind")
        choices = request.POST.getlist("choices")
        required = set(request.POST.getlist("required"))
        keys = request.POST.getlist("key")
        rows = []
        for i, label in enumerate(labels):
            if not label.strip():
                continue
            rows.append(
                {
                    "key": keys[i] if i < len(keys) else "",
                    "label": label,
                    "kind": kinds[i] if i < len(kinds) else FieldKind.TEXT,
                    "required": str(i) in required,
                    "choices": [c for c in (choices[i] if i < len(choices) else "").split(",")],
                }
            )
        try:
            form = services.save_form(
                request.association,
                name=request.POST.get("name", ""),
                description=request.POST.get("description", "").strip()[:2000],
                active=request.POST.get("active") == "on",
                fields=rows,
                form=form,
                statutory_kind=request.POST.get("statutory_kind", ""),
            )
            messages.success(request, _("Form saved."))
            return redirect("arc:form_edit", form.pk)
        except services.ArcError as exc:
            messages.error(request, str(exc))
    shown = [{**r, "choices_text": ", ".join(r.get("choices") or [])} for r in rows] + [
        {"key": "", "label": "", "kind": "text", "required": False, "choices_text": ""}
    ] * EXTRA_ROWS
    return render(
        request,
        "arc/form_edit.html",
        {
            "form": form,
            "rows": shown,
            "kinds": FieldKind.choices,
            "statutory_kinds": StatutoryKind.choices,
            "statutory_kind": request.POST.get(
                "statutory_kind", form.statutory_kind if form else ""
            ),
            "name": request.POST.get("name", form.name if form else ""),
        },
    )


@require_POST
@require_perm(perms.ARCH_DECIDE)
def reviewers_set(request):
    from homeoa.governance.models import Committee

    pk = request.POST.get("committee", "")
    committee = Committee.objects.filter(pk=pk, active=True).first() if pk.isdigit() else None
    services.set_reviewer_committee(request.association, committee)
    messages.success(request, _("Reviewers saved."))
    return redirect("arc:forms")
