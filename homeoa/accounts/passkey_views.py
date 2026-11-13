"""Passkey views (PLT-02): manage passkeys under /account/passkeys/, sign in at /login/.

The browser side (``accounts/passkeys.js``) asks for options, runs the WebAuthn ceremony
and posts the result as JSON. Challenges live in the session and are used once.
"""

import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from . import passkeys, services, signals
from .models import PasskeyCredential
from .policy import require_mfa_for_board
from .views import (
    ENROL_REQUIRED,
    MAX_MFA_FAILS,
    PENDING_FAILS,
    PENDING_USER,
    login_and_target,
)

REG_CHALLENGE = "accounts_passkey_reg_challenge"
AUTH_CHALLENGE = "accounts_passkey_auth_challenge"
NEW_CODES = "accounts_passkey_new_codes"

ERRORS = {
    "invalid": gettext_lazy("That passkey couldn't be verified. Try again."),
    "unknown": gettext_lazy(
        "That passkey isn't registered here. Sign in with an email link instead."
    ),
    "counter": gettext_lazy(
        "That passkey looks like a copy and was refused. Sign in with an email link."
    ),
    "duplicate": gettext_lazy("That passkey is already registered."),
}


def _error(reason, status=400):
    return JsonResponse({"error": str(ERRORS.get(reason, ERRORS["invalid"]))}, status=status)


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _pending_user(request):
    pk = request.session.get(PENDING_USER)
    return get_user_model().objects.filter(pk=pk, is_active=True).first() if pk else None


# --- sign in -------------------------------------------------------------------------


@require_POST
def login_options(request):
    """Options for a passkey sign-in, or for the second step after an email link."""
    user = _pending_user(request)
    options, challenge = passkeys.authentication_options(passkeys.relying_party(request), user)
    request.session[AUTH_CHALLENGE] = challenge
    return JsonResponse(json.loads(options))


@require_POST
def login_finish(request):
    challenge = request.session.pop(AUTH_CHALLENGE, None)
    credential = _body(request).get("credential")
    pending = _pending_user(request)
    try:
        pk = passkeys.authenticate(
            credential, challenge, passkeys.relying_party(request), user=pending
        )
    except passkeys.PasskeyError as exc:
        claimed = pending or (passkeys.credential_owner(credential) if credential else None)
        signals.passkey_failed.send(sender=None, user=claimed, request=request, reason=exc.reason)
        if pending is not None:
            fails = request.session.get(PENDING_FAILS, 0) + 1
            if fails >= MAX_MFA_FAILS:
                signals.mfa_lockout.send(sender=None, user=pending, request=request)
                request.session.flush()
                return JsonResponse({"redirect": reverse("accounts:login")}, status=400)
            request.session[PENDING_FAILS] = fails
        return _error(exc.reason)
    user = pk.user
    if pending is not None:
        request.session.pop(PENDING_USER, None)
        request.session.pop(PENDING_FAILS, None)
        signals.mfa_challenge_passed.send(sender=None, user=user, request=request, method="passkey")
    else:
        signals.passkey_login.send(sender=None, user=user, request=request, passkey=pk)
    return JsonResponse({"redirect": login_and_target(request, user)})


# --- manage --------------------------------------------------------------------------


def _required(request, user) -> bool:
    return request.session.get(ENROL_REQUIRED, False) or require_mfa_for_board(
        user, getattr(request, "association", None)
    )


@login_required
@require_http_methods(["GET", "POST"])
def manage(request):
    user = request.user
    error = None
    if request.method == "POST":
        pk = get_object_or_404(PasskeyCredential, pk=request.POST.get("id"), user=user)
        action = request.POST.get("action")
        if action == "rename":
            name = request.POST.get("name", "").strip()[:100]
            if name:
                pk.name = name
                pk.save(update_fields=["name"])
                signals.passkey_renamed.send(sender=None, user=user, request=request, passkey=pk)
                return redirect("accounts:passkeys")
            error = _("Give the passkey a name.")
        elif action == "remove":
            last_factor = not user.has_totp and user.passkeys.count() == 1
            if last_factor and _required(request, user):
                error = _(
                    "Your association requires two-step sign-in for board members. "
                    "Add another passkey or an authenticator app before removing this one."
                )
            else:
                pk.delete()
                if not user.has_mfa:
                    user.recovery_codes.all().delete()  # no second factor left to recover
                signals.passkey_removed.send(sender=None, user=user, request=request, passkey=pk)
                return redirect("accounts:passkeys")
    return render(
        request,
        "accounts/passkeys.html",
        {
            "codes": request.session.pop(NEW_CODES, None),  # shown once
            "passkeys": user.passkeys.all(),
            "required": request.session.get(ENROL_REQUIRED, False),
            "error": error,
        },
    )


@login_required
@require_POST
def register_options(request):
    options, challenge = passkeys.registration_options(
        request.user, passkeys.relying_party(request)
    )
    request.session[REG_CHALLENGE] = challenge
    return JsonResponse(json.loads(options))


@login_required
@require_POST
def register_finish(request):
    data = _body(request)
    challenge = request.session.pop(REG_CHALLENGE, None)
    try:
        pk = passkeys.register(
            request.user,
            data.get("credential"),
            challenge,
            passkeys.relying_party(request),
            name=str(data.get("name") or "") or _("Passkey"),
        )
    except passkeys.PasskeyError as exc:
        return _error(exc.reason)
    request.session.pop(ENROL_REQUIRED, None)
    signals.passkey_registered.send(sender=None, user=request.user, request=request, passkey=pk)
    if not request.user.recovery_codes.exists():
        # First second factor: issue recovery codes, as TOTP setup does.
        request.session[NEW_CODES] = services.regenerate_recovery_codes(request.user)
        signals.recovery_codes_regenerated.send(sender=None, user=request.user, request=request)
    return JsonResponse({"redirect": reverse("accounts:passkeys")})
