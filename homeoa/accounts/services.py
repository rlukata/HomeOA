"""Token, rate-limit and MFA services. Views and the e2e commands call only these."""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import pyotp
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import LoginRequest, MagicLinkToken, RecoveryCode, TOTPDevice, User

LINK_TTL = timedelta(minutes=15)
RATE_WINDOW = timedelta(minutes=15)
RATE_PER_EMAIL = getattr(settings, "HOMEOA_LOGIN_RATE_PER_EMAIL", 5)
RATE_PER_IP = getattr(settings, "HOMEOA_LOGIN_RATE_PER_IP", 20)
RECOVERY_CODE_COUNT = 10
TOTP_ISSUER = "HomeOA"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# --- magic links ---------------------------------------------------------------------


def create_magic_link(user: User, ip: str | None = None) -> str:
    """Create a token for ``user`` and return the raw value (never stored)."""
    raw = secrets.token_urlsafe(24)  # 192 bits; short enough to survive mail line wrapping
    MagicLinkToken.objects.create(user=user, token_hash=_sha256(raw), request_ip=ip)
    return raw


@dataclass
class LinkResult:
    user: User | None
    reason: str | None = None  # None on success, else "invalid" | "used" | "expired"


def _check(token: MagicLinkToken | None) -> LinkResult:
    if token is None or not token.user.is_active:
        return LinkResult(None, "invalid")
    if token.used_at is not None:
        return LinkResult(token.user, "used")
    if token.created_at < timezone.now() - LINK_TTL:
        return LinkResult(token.user, "expired")
    return LinkResult(token.user)


def peek_magic_link(raw: str) -> LinkResult:
    """Check a token without using it (GET of the link, which scanners may prefetch)."""
    qs = MagicLinkToken.objects.select_related("user").filter(token_hash=_sha256(raw or ""))
    return _check(qs.first())


def consume_magic_link(raw: str) -> LinkResult:
    """Mark the token used and return its user, or the reason it was refused."""
    with transaction.atomic():
        token = (
            MagicLinkToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=_sha256(raw or ""))
            .first()
        )
        result = _check(token)
        if result.reason:
            return result
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        return LinkResult(token.user)


def mask_email(email: str) -> str:
    local, _sep, domain = email.partition("@")
    return f"{local[:2]}{'•' * max(len(local) - 2, 1)}@{domain}"


def age_outstanding_links(minutes: int) -> int:
    """Backdate unused tokens by ``minutes`` (test-only; see e2e_age_magic_links)."""
    count = 0
    for token in MagicLinkToken.objects.filter(used_at__isnull=True):
        token.created_at -= timedelta(minutes=minutes)
        token.save(update_fields=["created_at"])
        count += 1
    return count


def record_login_request(email: str, ip: str | None) -> bool:
    """Log the attempt; return True when it is within the rate limit."""
    since = timezone.now() - RATE_WINDOW
    email = normalize_email(email)
    by_email = LoginRequest.objects.filter(email=email, created_at__gte=since).count()
    by_ip = LoginRequest.objects.filter(ip=ip, created_at__gte=since).count() if ip else 0
    LoginRequest.objects.create(email=email, ip=ip)
    return by_email < RATE_PER_EMAIL and by_ip < RATE_PER_IP


# --- TOTP ------------------------------------------------------------------------------


def start_enrolment(user: User) -> TOTPDevice:
    """Return the user's unconfirmed device, creating one with a fresh secret."""
    device, created = TOTPDevice.objects.get_or_create(
        user=user, defaults={"secret": pyotp.random_base32()}
    )
    if not created and device.confirmed_at is None and not device.secret:
        device.secret = pyotp.random_base32()
        device.save(update_fields=["secret"])
    return device


def provisioning_uri(device: TOTPDevice) -> str:
    return pyotp.TOTP(device.secret).provisioning_uri(
        name=device.user.email, issuer_name=TOTP_ISSUER
    )


def verify_totp(device: TOTPDevice, code: str, now: float | None = None) -> bool:
    """Accept the current step or one step either side; each step works only once."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return False
    totp = pyotp.TOTP(device.secret)
    now = time.time() if now is None else now
    current = int(now) // totp.interval
    for step in (current - 1, current, current + 1):
        if step <= device.last_used_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            device.last_used_step = step
            device.save(update_fields=["last_used_step"])
            return True
    return False


def confirm_enrolment(device: TOTPDevice, code: str, now: float | None = None) -> list[str]:
    """Confirm the device with a valid code and return fresh recovery codes, or []."""
    if not verify_totp(device, code, now):
        return []
    device.confirmed_at = timezone.now()
    device.save(update_fields=["confirmed_at"])
    return regenerate_recovery_codes(device.user)


def disable_mfa(user: User) -> None:
    TOTPDevice.objects.filter(user=user).delete()
    RecoveryCode.objects.filter(user=user).delete()


# --- recovery codes ------------------------------------------------------------------

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def regenerate_recovery_codes(user: User) -> list[str]:
    RecoveryCode.objects.filter(user=user).delete()
    codes = []
    for _i in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_ALPHABET) for _j in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
        RecoveryCode.objects.create(user=user, code_hash=_sha256(raw))
    return codes


def use_recovery_code(user: User, code: str) -> bool:
    with transaction.atomic():
        rc = (
            RecoveryCode.objects.select_for_update()
            .filter(user=user, used_at__isnull=True, code_hash=_sha256(_normalize_code(code)))
            .first()
        )
        if rc is None:
            return False
        rc.used_at = timezone.now()
        rc.save(update_fields=["used_at"])
        return True
