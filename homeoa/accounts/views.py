import base64

import segno
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from homeoa.access.services import residents_enabled
from homeoa.core.logging import client_ip as _client_ip

from . import services, signals, tasks
from .models import TOTPDevice
from .policy import require_mfa_for_board

PENDING_USER = "accounts_mfa_pending_user"
PENDING_FAILS = "accounts_mfa_pending_fails"
ENROL_REQUIRED = "accounts_mfa_enrol_required"
NEXT_URL = "accounts_next"
MAX_MFA_FAILS = 5


def client_ip(request) -> str | None:
    # X-Forwarded-For only from HOMEOA_TRUSTED_PROXIES.
    return _client_ip(request)


def _safe_next(request, url):
    if url and url_has_allowed_host_and_scheme(
        url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return url
    return None


def login_and_target(request, user) -> str:
    """Log in (rotating the session key) and return where to send the user."""
    next_url = request.session.get(NEXT_URL)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    if require_mfa_for_board(user, getattr(request, "association", None)) and not user.has_mfa:
        request.session[ENROL_REQUIRED] = True
        return reverse("accounts:mfa_setup")
    return _safe_next(request, next_url) or reverse("dashboard:index")


def _finish_login(request, user):
    return redirect(login_and_target(request, user))


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.method == "GET":
        if nxt := _safe_next(request, request.GET.get("next")):
            request.session[NEXT_URL] = nxt
        prefill = request.GET.get("email", "")[:254]  # from a resident invitation
        return render(request, "accounts/login.html", {"email": prefill})

    email = services.normalize_email(request.POST.get("email", ""))
    if "@" not in email:
        return render(
            request,
            "accounts/login.html",
            {"error": _("Enter a valid email address."), "email": email},
            status=400,
        )
    ip = client_ip(request)
    allowed = services.record_login_request(email, ip)
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
    signals.login_link_requested.send(
        sender=None, email=email, ip=ip, user=user, throttled=not allowed
    )
    if not allowed:
        return render(request, "accounts/login.html", {"throttled": True}, status=429)
    # Queued for every address, known or not: the job decides whether to send, so the
    # response (and its timing) never depends on whether the account exists.
    tasks.queue_magic_link(email=email, ip=ip, origin=request.build_absolute_uri("/")[:-1])
    # Same page whether or not the address exists.
    # The same note for every address while residents are off, so nothing leaks.
    association = getattr(request, "association", None)
    closed = association is not None and not residents_enabled(association)
    return render(request, "accounts/login_sent.html", {"email": email, "residents_closed": closed})


@require_http_methods(["GET", "POST"])
def verify_view(request, token: str):
    """GET only shows a confirm button: mail scanners prefetch links. POST signs in."""
    if request.method == "GET":
        result = services.peek_magic_link(token)
        if not result.reason:
            return render(
                request,
                "accounts/verify_confirm.html",
                {"masked_email": services.mask_email(result.user.email)},
            )
    else:
        result = services.consume_magic_link(token)
    if result.reason:
        signals.login_link_rejected.send(
            sender=None, reason=result.reason, ip=client_ip(request), user=result.user
        )
        return render(request, "accounts/link_invalid.html", {"reason": result.reason}, status=400)
    user = result.user
    if user.has_mfa:
        request.session.cycle_key()
        request.session[PENDING_USER] = user.pk
        request.session[PENDING_FAILS] = 0
        return redirect("accounts:mfa_challenge")
    return _finish_login(request, user)


@require_http_methods(["GET", "POST"])
def mfa_challenge_view(request):
    pk = request.session.get(PENDING_USER)
    user = get_user_model().objects.filter(pk=pk, is_active=True).first() if pk else None
    if user is None:
        return redirect("accounts:login")
    error = None
    if request.method == "POST":
        code = request.POST.get("code", "")
        device = TOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).first()
        method = None
        if device and services.verify_totp(device, code):
            method = "totp"
        elif services.use_recovery_code(user, code):
            method = "recovery"
        if method:
            request.session.pop(PENDING_USER, None)
            request.session.pop(PENDING_FAILS, None)
            signals.mfa_challenge_passed.send(
                sender=None, user=user, request=request, method=method
            )
            return _finish_login(request, user)
        signals.mfa_challenge_failed.send(sender=None, user=user, request=request)
        fails = request.session.get(PENDING_FAILS, 0) + 1
        if fails >= MAX_MFA_FAILS:
            signals.mfa_lockout.send(sender=None, user=user, request=request)
            request.session.flush()
            return render(request, "accounts/link_invalid.html", {"reason": "mfa"}, status=400)
        request.session[PENDING_FAILS] = fails
        error = _("That code didn't work. Try again.")
    return render(
        request,
        "accounts/mfa_challenge.html",
        {
            "error": error,
            "has_totp": user.has_totp,
            "has_codes": user.has_totp or user.recovery_codes.filter(used_at__isnull=True).exists(),
            "has_passkey": user.passkeys.exists(),
        },
    )


def _qr_data_uri(uri: str) -> str:
    svg = segno.make(uri, error="m").svg_inline(scale=5, border=2)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


@login_required
@require_http_methods(["GET", "POST"])
def mfa_setup_view(request):
    user = request.user
    action = request.POST.get("action") if request.method == "POST" else None
    enrolled = user.has_totp
    required = request.session.get(ENROL_REQUIRED, False) or require_mfa_for_board(
        user, getattr(request, "association", None)
    )

    if enrolled:
        ctx = {"enrolled": True, "required": required and not user.passkeys.exists()}
        if action == "regenerate":
            ctx["codes"] = services.regenerate_recovery_codes(user)
            signals.recovery_codes_regenerated.send(sender=None, user=user, request=request)
        elif action == "disable" and (not required or user.passkeys.exists()):
            if user.passkeys.exists():  # keep recovery codes; they back up the passkeys
                TOTPDevice.objects.filter(user=user).delete()
            else:
                services.disable_mfa(user)
            signals.mfa_disabled.send(sender=None, user=user, request=request)
            return redirect("accounts:mfa_setup")
        else:
            ctx["remaining"] = user.recovery_codes.filter(used_at__isnull=True).count()
        return render(request, "accounts/mfa_setup.html", ctx)

    device = services.start_enrolment(user)
    error = None
    if action == "confirm":
        codes = services.confirm_enrolment(device, request.POST.get("code", ""))
        if codes:
            request.session.pop(ENROL_REQUIRED, None)
            signals.mfa_enrolled.send(sender=None, user=user, request=request)
            return render(
                request,
                "accounts/mfa_setup.html",
                {"enrolled": True, "codes": codes, "just_enrolled": True},
            )
        error = _("That code didn't work. Check your authenticator's time and try again.")
    return render(
        request,
        "accounts/mfa_setup.html",
        {
            "enrolled": False,
            "required": required,
            "secret": device.secret,
            "qr": _qr_data_uri(services.provisioning_uri(device)),
            "error": error,
        },
    )


@require_http_methods(["GET", "POST"])
def logout_view(request):
    if request.method == "POST":
        logout(request)
        return render(request, "accounts/logged_out.html")
    if not request.user.is_authenticated:
        return redirect("accounts:login")
    return render(request, "accounts/logout.html")
