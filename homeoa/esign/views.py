from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.core.validators import validate_email
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.logging import client_ip
from homeoa.providers import get_provider

from . import services
from .models import Envelope, SignatureEvent, Signer

MAX_SIGNERS = 6


def _can_manage(request) -> bool:
    return has_perm(request.user, perms.DOCUMENTS_MANAGE, association=request.association)


def _manage(request) -> None:
    if not _can_manage(request):
        raise PermissionDenied


def _base_url(request) -> str:
    return request.build_absolute_uri("/")


@require_perm(perms.BOARD_SHELL)
def index(request):
    return render(
        request,
        "esign/index.html",
        {
            "envelopes": Envelope.objects.prefetch_related("signers"),
            "can_manage": _can_manage(request),
        },
    )


@require_perm(perms.BOARD_SHELL)
def new(request):
    _manage(request)
    rows = [{"name": "", "email": ""} for _i in range(MAX_SIGNERS)]
    form = {"title": "", "message": "", "routing": Envelope.Routing.SEQUENTIAL}
    errors = []
    if request.method == "POST":
        form = {k: request.POST.get(k, "").strip() for k in form}
        signers = []
        User = get_user_model()
        for i in range(MAX_SIGNERS):
            name = request.POST.get(f"name_{i}", "").strip()
            email = request.POST.get(f"email_{i}", "").strip()
            rows[i] = {"name": name, "email": email}
            if not (name or email):
                continue
            try:
                validate_email(email)
            except Exception:
                errors.append(_("Enter a valid email for %(name)s.") % {"name": name or email})
                continue
            if not name:
                errors.append(_("Enter a name for %(email)s.") % {"email": email})
                continue
            user = User.objects.filter(email__iexact=email).first()
            signers.append({"name": name, "email": email, "user": user})
        upload = request.FILES.get("file")
        if not form["title"]:
            errors.append(_("Enter a title."))
        if upload is None or not upload.name.lower().endswith(".pdf"):
            errors.append(_("Choose a PDF file to sign."))
        if not signers:
            errors.append(_("Add at least one signer."))
        if form["routing"] not in Envelope.Routing.values:
            form["routing"] = Envelope.Routing.SEQUENTIAL
        if not errors:
            content = upload.read()
            if not content.startswith(b"%PDF"):
                errors.append(_("Choose a PDF file to sign."))
            else:
                stored = get_provider("FileStorage", request.association).put(
                    request.association.pk,
                    upload.name,
                    content,
                    content_type="application/pdf",
                    folder="esign",
                )
                env = services.create_envelope(
                    request.association,
                    title=form["title"],
                    message=form["message"],
                    routing=form["routing"],
                    source_key=stored.key,
                    source_name=upload.name,
                    signers=signers,
                    user=request.user,
                )
                return redirect("esign:detail", pk=env.pk)
    return render(
        request,
        "esign/new.html",
        {
            "form": form,
            "rows": rows,
            "errors": errors,
            "routings": Envelope.Routing.choices,
        },
    )


@require_perm(perms.BOARD_SHELL)
def detail(request, pk):
    env = get_object_or_404(Envelope.objects, pk=pk)
    events = list(env.events.select_related("signer"))
    # A signer's invite failed if their latest invite attempt has a failure after it.
    last_invite = {}
    for ev in events:
        if ev.kind in (SignatureEvent.Kind.INVITED, SignatureEvent.Kind.INVITE_FAILED):
            last_invite[ev.signer_id] = ev.kind
    failed = {k for k, v in last_invite.items() if v == SignatureEvent.Kind.INVITE_FAILED}
    signers = list(env.signers.all())
    for s in signers:
        s.invite_failed = s.pk in failed
    return render(
        request,
        "esign/detail.html",
        {
            "envelope": env,
            "signers": signers,
            "events": events,
            "can_manage": _can_manage(request),
            "needs_review": env.status in (Envelope.Status.COMPLETED, Envelope.Status.DECLINED)
            and not any(ev.kind == SignatureEvent.Kind.REVIEWED for ev in events),
        },
    )


@require_POST
@require_perm(perms.BOARD_SHELL)
def send(request, pk):
    _manage(request)
    env = get_object_or_404(Envelope.objects, pk=pk)
    try:
        services.send(env, base_url=_base_url(request))
        messages.success(request, _("Sent for signature."))
    except services.ESignError as exc:
        messages.error(request, str(exc))
    return redirect("esign:detail", pk=pk)


@require_POST
@require_perm(perms.BOARD_SHELL)
def void(request, pk):
    _manage(request)
    env = get_object_or_404(Envelope.objects, pk=pk)
    try:
        services.void(env, reason=request.POST.get("reason", "").strip())
        messages.success(request, _("Envelope voided. Its signing links no longer work."))
    except services.ESignError as exc:
        messages.error(request, str(exc))
    return redirect("esign:detail", pk=pk)


@require_POST
@require_perm(perms.BOARD_SHELL)
def reviewed(request, pk):
    _manage(request)
    env = get_object_or_404(Envelope.objects, pk=pk)
    try:
        services.mark_reviewed(env, user=request.user)
        messages.success(request, _("Noted. The envelope is off the to-do list."))
    except services.ESignError as exc:
        messages.error(request, str(exc))
    return redirect("esign:detail", pk=pk)


@require_POST
@require_perm(perms.BOARD_SHELL)
def resend(request, pk, signer):
    _manage(request)
    s = get_object_or_404(Signer.objects, pk=signer, envelope_id=pk)
    try:
        services.resend(s, base_url=_base_url(request))
        messages.success(request, _("A new signing link was emailed."))
    except services.ESignError as exc:
        messages.error(request, str(exc))
    return redirect("esign:detail", pk=pk)


@require_perm(perms.BOARD_SHELL)
def file(request, pk, which):
    env = get_object_or_404(Envelope.objects, pk=pk)
    key = {"original": env.source_key, "signed": env.result_key}.get(which)
    if not key:
        raise Http404
    name = env.source_name if which == "original" else f"{env.title}-signed.pdf"
    url = get_provider("FileStorage", request.association).signed_url(
        key, association_id=env.association_id, filename=name
    )
    return redirect(url)


# --- signer pages (no board permission; the single-use link is the capability) ----------


def _signer_or_page(request, token):
    try:
        signer = services.signer_for_token(token)
    except services.LinkInvalid as exc:
        return None, render(request, "esign/link_invalid.html", {"reason": str(exc)}, status=410)
    if signer.user_id:
        if not request.user.is_authenticated:
            return None, redirect_to_login(request.get_full_path())
        try:
            services.check_user(signer, request.user)
        except services.LinkInvalid as exc:
            return None, render(
                request, "esign/link_invalid.html", {"reason": str(exc)}, status=403
            )
    return signer, None


def sign(request, token):
    resp = _sign(request, token)
    # same-origin, not no-referrer: with no-referrer Chrome sends Origin: null on the form
    # POST and Django's CSRF check refuses it. same-origin still keeps the token off other sites.
    resp["Referrer-Policy"] = "same-origin"
    resp["Cache-Control"] = "no-store"
    return resp


def _sign(request, token):
    signer, page = _signer_or_page(request, token)
    if page is not None:
        return page
    ip = client_ip(request)
    ua = request.META.get("HTTP_USER_AGENT", "")
    error = ""
    if request.method == "POST":
        try:
            if request.POST.get("action") == "decline":
                services.decline(
                    token,
                    reason=request.POST.get("reason", "").strip(),
                    ip=ip,
                    user_agent=ua,
                    user=request.user,
                )
                return render(request, "esign/done.html", {"declined": True, "signer": signer})
            services.sign(
                token,
                typed_name=request.POST.get("typed_name", ""),
                consent=request.POST.get("consent") == "on",
                ip=ip,
                user_agent=ua,
                user=request.user,
                base_url=_base_url(request),
            )
            return render(request, "esign/done.html", {"declined": False, "signer": signer})
        except services.ESignError as exc:
            error = str(exc)
    else:
        services.record_view(signer, ip=ip, user_agent=ua)
    return render(
        request,
        "esign/sign.html",
        {
            "signer": signer,
            "envelope": signer.envelope,
            "token": token,
            "error": error,
            "is_turn": services.is_turn(signer),
        },
    )


def sign_document(request, token):
    signer, page = _signer_or_page(request, token)
    if page is not None:
        return page
    env = signer.envelope
    content = get_provider("FileStorage", env.association).get(env.source_key)
    resp = HttpResponse(content, content_type="application/pdf")
    resp["Content-Disposition"] = 'inline; filename="document.pdf"'
    resp["Referrer-Policy"] = "no-referrer"
    resp["Cache-Control"] = "no-store"
    return resp
