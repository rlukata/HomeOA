"""WebAuthn passkey services (PLT-02), built on py_webauthn.

Views keep the pending challenge in the session and call only these functions. Passkeys
always require user verification (PIN or biometric on the device), so a passkey sign-in
counts as multi-factor for the board MFA policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .models import PasskeyCredential, User

RP_NAME = "HomeOA"


class PasskeyError(Exception):
    """Registration or assertion was refused. ``reason`` is a short machine code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RelyingParty:
    rp_id: str
    origin: str


def relying_party(request) -> RelyingParty:
    """RP ID and origin for WebAuthn.

    ``HOMEOA_WEBAUTHN_RP_ID``/``_ORIGIN`` always win. With ``HOMEOA_WEBAUTHN_HOST_FALLBACK``
    off (production) the rest comes from ``HOMEOA_BASE_URL``, so a spoofed Host header can
    never choose the relying party. With it on (dev, tests, e2e) the request host is used.
    """
    rp_id = getattr(settings, "HOMEOA_WEBAUTHN_RP_ID", "")
    origin = getattr(settings, "HOMEOA_WEBAUTHN_ORIGIN", "")
    fallback = getattr(settings, "HOMEOA_WEBAUTHN_HOST_FALLBACK", True)
    base = getattr(settings, "HOMEOA_BASE_URL", "")
    if not fallback and base and not (rp_id and origin):
        parsed = urlsplit(base)
        if parsed.scheme and parsed.hostname:
            rp_id = rp_id or parsed.hostname
            origin = origin or f"{parsed.scheme}://{parsed.netloc}"
    if not (rp_id and origin):
        if not fallback:
            raise ImproperlyConfigured(
                "Passkeys need HOMEOA_BASE_URL (or HOMEOA_WEBAUTHN_RP_ID and _ORIGIN)."
            )
        host = request.get_host()
        rp_id = rp_id or host.rsplit(":", 1)[0]
        origin = origin or f"{'https' if request.is_secure() else 'http'}://{host}"
    return RelyingParty(rp_id=rp_id, origin=origin)


def _descriptors(user: User) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(id=bytes(pk.credential_id))
        for pk in PasskeyCredential.objects.filter(user=user)
    ]


def registration_options(user: User, rp: RelyingParty) -> tuple[str, str]:
    """Return (options JSON for the browser, challenge to keep in the session)."""
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        user_id=str(user.pk).encode(),
        user_name=user.email,
        user_display_name=user.full_name or user.email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_descriptors(user),
    )
    return options_to_json(options), bytes_to_base64url(options.challenge)


def register(user: User, credential, challenge: str, rp: RelyingParty, name: str):
    """Verify a registration response and store the credential."""
    if not challenge or not isinstance(credential, dict):
        raise PasskeyError("invalid")
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origin,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise PasskeyError("invalid") from exc
    if PasskeyCredential.objects.filter(credential_id=verified.credential_id).exists():
        raise PasskeyError("duplicate")
    transports = (credential.get("response") or {}).get("transports") or []
    return PasskeyCredential.objects.create(
        user=user,
        name=(name or "").strip()[:100] or "Passkey",
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[str(t) for t in transports][:10],
    )


def authentication_options(rp: RelyingParty, user: User | None = None) -> tuple[str, str]:
    """Options for sign-in. With ``user`` (second step), only their passkeys are allowed."""
    options = generate_authentication_options(
        rp_id=rp.rp_id,
        allow_credentials=_descriptors(user) if user is not None else None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json(options), bytes_to_base64url(options.challenge)


def credential_owner(credential) -> User | None:
    """The active user a submitted assertion claims to be, without verifying it."""
    try:
        raw_id = base64url_to_bytes(credential["rawId"])
    except KeyError, TypeError, ValueError:
        return None
    pk = PasskeyCredential.objects.filter(credential_id=raw_id).select_related("user").first()
    return pk.user if pk is not None and pk.user.is_active else None


def authenticate(credential, challenge: str, rp: RelyingParty, user: User | None = None):
    """Verify an assertion and return the matching ``PasskeyCredential``.

    Raises ``PasskeyError`` with reason "invalid", "unknown" or "counter" (a sign count
    that did not go up, which can mean a cloned authenticator).
    """
    if not challenge or not isinstance(credential, dict):
        raise PasskeyError("invalid")
    try:
        raw_id = base64url_to_bytes(credential["rawId"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PasskeyError("invalid") from exc
    with transaction.atomic():
        qs = PasskeyCredential.objects.select_for_update().select_related("user")
        pk = qs.filter(credential_id=raw_id, user__is_active=True).first()
        if pk is None or (user is not None and pk.user_id != user.pk):
            raise PasskeyError("unknown")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge),
                expected_rp_id=rp.rp_id,
                expected_origin=rp.origin,
                credential_public_key=bytes(pk.public_key),
                credential_current_sign_count=pk.sign_count,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as exc:
            if "sign count" in str(exc).lower():
                raise PasskeyError("counter") from exc
            raise PasskeyError("invalid") from exc
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            raise PasskeyError("invalid") from exc
        pk.sign_count = verified.new_sign_count
        pk.last_used_at = timezone.now()
        pk.save(update_fields=["sign_count", "last_used_at"])
        return pk
