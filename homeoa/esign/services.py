"""Click-to-sign workflow (GOV-08).

Lifecycle: ``create_envelope`` (draft) -> ``send`` -> each signer ``sign``s through a
single-use emailed link -> completed (signed PDF = original + certificate page). Any
signer may ``decline``; the board may ``void`` an open envelope.

Tokens: 32 random bytes, url-safe; only their SHA-256 is stored. A link works once (signing
or declining uses it) and expires after ``HOMEOA_ESIGN_LINK_DAYS`` (default 14). With
``routing=sequential`` only the first unsigned signer is invited; the next one is invited
when the previous signs. With ``parallel`` everyone is invited at send.

Before every signature the stored source file is re-hashed and compared with the hash taken
at send; a mismatch refuses the signature (``TamperedError``) and logs a ``tamper`` event.

On completion ``envelope.on_complete`` (dotted path to ``callable(envelope)``), if set, is
called after commit, e.g. so minutes can file the result with ``documents.attach``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.notify import types as ntypes

from . import pdf
from .models import Envelope, SignatureEvent, Signer

logger = logging.getLogger(__name__)

INVITE_TYPE = "esign.invite"


class ESignError(ValueError):
    pass


class LinkInvalid(ESignError):
    pass


class TamperedError(ESignError):
    pass


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=INVITE_TYPE,
            label=gettext_lazy("Request to sign a document"),
            default_channels=("email",),
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with a=association_name t=title %}"
                "{{ a }}: please sign {{ t }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=signer_name %}Hello {{ n }},"
                "{% endblocktranslate %}\n\n"
                "{% blocktranslate with a=association_name t=title %}{{ a }} asks you to "
                "sign {{ t }}.{% endblocktranslate %}\n\n"
                "{% if message %}{{ message }}\n\n{% endif %}"
                "{% translate 'Review and sign here (the link works once):' %}\n{{ link }}\n\n"
                "{% blocktranslate with d=expires %}The link expires on {{ d }}."
                "{% endblocktranslate %}\n"
            ),
        )
    )


def link_days() -> int:
    return int(getattr(settings, "HOMEOA_ESIGN_LINK_DAYS", 14))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_token(token: str) -> str:
    return sha256(token.encode())


def _storage(association):
    from homeoa.providers import get_provider

    return get_provider("FileStorage", association)


def _event(envelope, kind, signer=None, *, ip=None, user_agent="", **data) -> SignatureEvent:
    return SignatureEvent.all_associations.create(
        association_id=envelope.association_id,
        envelope=envelope,
        signer=signer,
        kind=kind,
        ip=ip or None,
        user_agent=(user_agent or "")[:500],
        data=data,
    )


# --- board side ---------------------------------------------------------------------------


def create_envelope(
    association,
    *,
    title: str,
    source_key: str,
    signers: list[dict],
    source_name: str = "",
    message: str = "",
    routing: str = Envelope.Routing.SEQUENTIAL,
    on_complete: str = "",
    on_complete_ref: str = "",
    user=None,
) -> Envelope:
    """Create a draft. ``signers`` are ``{"name", "email", "user"?}`` in signing order."""
    if not signers:
        raise ESignError(_("Add at least one signer."))
    if not str(source_key).startswith(f"{association.pk}/"):
        raise ESignError(_("The file does not belong to this association."))
    if not _storage(association).exists(source_key):
        raise ESignError(_("The file to sign was not found."))
    with transaction.atomic():
        env = Envelope.all_associations.create(
            association=association,
            title=title,
            message=message,
            source_key=source_key,
            source_name=source_name,
            routing=routing,
            on_complete=on_complete,
            on_complete_ref=on_complete_ref,
            created_by=user if getattr(user, "pk", None) else None,
        )
        for i, s in enumerate(signers, start=1):
            Signer.all_associations.create(
                association=association,
                envelope=env,
                order=i,
                name=s["name"],
                email=s["email"],
                user=s.get("user"),
            )
        _event(env, SignatureEvent.Kind.CREATED)
    return env


def send(envelope: Envelope, *, base_url: str = "") -> Envelope:
    """Hash the source, mark the envelope sent and invite the first signer(s)."""
    if envelope.status != Envelope.Status.DRAFT:
        raise ESignError(_("Only a draft can be sent."))
    content = _storage(envelope.association).get(envelope.source_key)
    with transaction.atomic():
        envelope.source_sha256 = sha256(content)
        envelope.status = Envelope.Status.SENT
        envelope.sent_at = timezone.now()
        envelope.save(update_fields=["source_sha256", "status", "sent_at"])
        _event(envelope, SignatureEvent.Kind.SENT, sha256=envelope.source_sha256)
        _invite_next(envelope, base_url=base_url)
    audit.record("esign.sent", envelope)
    return envelope


def void(envelope: Envelope, *, reason: str = "") -> Envelope:
    if not envelope.is_open:
        raise ESignError(_("This envelope is already closed."))
    with transaction.atomic():
        envelope.status = Envelope.Status.VOIDED
        envelope.save(update_fields=["status"])
        envelope.signers.filter(token_used_at__isnull=True).exclude(token_hash="").update(
            token_hash=""
        )
        _event(envelope, SignatureEvent.Kind.VOIDED, reason=reason)
    audit.record("esign.voided", envelope, message=reason)
    return envelope


def _invite_next(envelope: Envelope, *, base_url: str = "") -> list[Signer]:
    pending = [s for s in envelope.signers.all() if s.status != Signer.Status.SIGNED]
    if envelope.routing == Envelope.Routing.SEQUENTIAL:
        pending = pending[:1]
    invited = []
    for s in pending:
        if s.status == Signer.Status.WAITING:
            _invite(envelope, s, base_url=base_url)
            invited.append(s)
    return invited


def issue_token(signer: Signer) -> str:
    token = secrets.token_urlsafe(32)
    signer.token_hash = hash_token(token)
    signer.token_expires_at = timezone.now() + dt.timedelta(days=link_days())
    signer.token_used_at = None
    signer.status = Signer.Status.INVITED
    signer.save(update_fields=["token_hash", "token_expires_at", "token_used_at", "status"])
    return token


def _invite(envelope: Envelope, signer: Signer, *, base_url: str = "") -> None:
    from homeoa.notify.services import render
    from homeoa.providers.email import EmailRequest

    token = issue_token(signer)
    link = (base_url or getattr(settings, "HOMEOA_BASE_URL", "")).rstrip("/") + reverse(
        "esign:sign", args=[token]
    )
    msg = render(
        ntypes.get(INVITE_TYPE),
        {
            "association_name": envelope.association.name,
            "signer_name": signer.name,
            "title": envelope.title,
            "message": envelope.message,
            "link": link,
            "expires": signer.token_expires_at.date().isoformat(),
        },
    )
    request = EmailRequest(to=(signer.email,), subject=msg.subject, text=msg.body)
    with transaction.atomic():
        _event(envelope, SignatureEvent.Kind.INVITED, signer, email=signer.email)
        # The token and the INVITED event commit with the caller's transaction; the
        # email goes out only after commit, so a rolled-back send never mails a dead link.
        transaction.on_commit(lambda: _deliver(envelope, signer, request))


def _deliver(envelope: Envelope, signer: Signer, request) -> None:
    """Send one invite email after commit. A failure is logged and audited, never
    raised: the board sees it on the envelope page and can resend the link."""
    from homeoa import providers

    try:
        result = providers.get_provider("EmailChannel", envelope.association).send(request)
        if result.sent:
            return
        error = result.error or ""
    except Exception as exc:
        error = str(exc)
    logger.warning("esign invite for signer %s failed: %s", signer.pk, error)
    try:
        _event(envelope, SignatureEvent.Kind.INVITE_FAILED, signer, email=signer.email, error=error)
    except Exception:
        logger.exception("could not record failed esign invite for signer %s", signer.pk)
    try:
        audit.record("esign.invite_failed", envelope, message=error)
    except Exception:
        logger.exception("could not audit failed esign invite for signer %s", signer.pk)


def resend(signer: Signer, *, base_url: str = "") -> None:
    envelope = signer.envelope
    if envelope.status != Envelope.Status.SENT or signer.status != Signer.Status.INVITED:
        raise ESignError(_("This signer has no open invitation."))
    _invite(envelope, signer, base_url=base_url)


# --- signer side --------------------------------------------------------------------------


def signer_for_token(token: str) -> Signer:
    """The signer whose link this is, if the link can still be used; else ``LinkInvalid``."""
    if not token:
        raise LinkInvalid(_("This signing link is not valid."))
    signer = (
        Signer.all_associations.select_related("envelope", "envelope__association")
        .filter(token_hash=hash_token(token))
        .first()
    )
    if signer is None:
        raise LinkInvalid(_("This signing link is not valid."))
    if signer.token_used_at is not None:
        raise LinkInvalid(_("This signing link has already been used."))
    if signer.token_expires_at is None or signer.token_expires_at <= timezone.now():
        raise LinkInvalid(_("This signing link has expired. Ask the board for a new one."))
    if signer.envelope.status != Envelope.Status.SENT:
        raise LinkInvalid(_("This document is no longer out for signature."))
    return signer


def check_user(signer: Signer, user) -> None:
    if signer.user_id and getattr(user, "pk", None) != signer.user_id:
        raise LinkInvalid(_("Sign in as %(name)s to sign this document.") % {"name": signer.name})


def is_turn(signer: Signer) -> bool:
    if signer.envelope.routing == Envelope.Routing.PARALLEL:
        return True
    return (
        not signer.envelope.signers.filter(order__lt=signer.order)
        .exclude(status=Signer.Status.SIGNED)
        .exists()
    )


def record_view(signer: Signer, *, ip=None, user_agent="") -> None:
    _event(signer.envelope, SignatureEvent.Kind.VIEWED, signer, ip=ip, user_agent=user_agent)


def verify_source(envelope: Envelope) -> bytes:
    content = _storage(envelope.association).get(envelope.source_key)
    if sha256(content) != envelope.source_sha256:
        _event(envelope, SignatureEvent.Kind.TAMPER, found=sha256(content))
        audit.record("esign.tamper_detected", envelope)
        raise TamperedError(
            _("The document changed after it was sent, so it cannot be signed. Ask the board.")
        )
    return content


def sign(
    token: str,
    *,
    typed_name: str,
    consent: bool,
    ip=None,
    user_agent: str = "",
    user=None,
    base_url: str = "",
) -> Signer:
    typed_name = (typed_name or "").strip()
    if not consent:
        raise ESignError(_("Agree to sign electronically first."))
    if not typed_name:
        raise ESignError(_("Type your full name to sign."))
    signer = signer_for_token(token)
    check_user(signer, user)
    if not is_turn(signer):
        raise ESignError(_("An earlier signer has not signed yet."))
    verify_source(signer.envelope)  # outside the transaction so the tamper event is kept
    with transaction.atomic():
        Signer.all_associations.select_for_update().get(pk=signer.pk)
        signer = signer_for_token(token)  # re-check under the row lock
        envelope = signer.envelope
        now = timezone.now()
        _event(envelope, SignatureEvent.Kind.CONSENTED, signer, ip=ip, user_agent=user_agent)
        signer.status = Signer.Status.SIGNED
        signer.signed_name = typed_name[:200]
        signer.acted_at = now
        signer.token_used_at = now
        signer.ip = ip or None
        signer.user_agent = (user_agent or "")[:500]
        signer.save()
        _event(
            envelope,
            SignatureEvent.Kind.SIGNED,
            signer,
            ip=ip,
            user_agent=user_agent,
            typed_name=signer.signed_name,
            sha256=envelope.source_sha256,
        )
        audit.record("esign.signed", envelope, message=signer.email)
        if envelope.signers.exclude(status=Signer.Status.SIGNED).exists():
            _invite_next(envelope, base_url=base_url)
        else:
            _complete(envelope)
    return signer


def decline(token: str, *, reason: str = "", ip=None, user_agent: str = "", user=None) -> Signer:
    with transaction.atomic():
        signer = signer_for_token(token)
        check_user(signer, user)
        envelope = signer.envelope
        now = timezone.now()
        signer.status = Signer.Status.DECLINED
        signer.decline_reason = reason
        signer.acted_at = signer.token_used_at = now
        signer.ip = ip or None
        signer.user_agent = (user_agent or "")[:500]
        signer.save()
        envelope.status = Envelope.Status.DECLINED
        envelope.save(update_fields=["status"])
        _event(
            envelope,
            SignatureEvent.Kind.DECLINED,
            signer,
            ip=ip,
            user_agent=user_agent,
            reason=reason,
        )
        audit.record("esign.declined", envelope, message=signer.email)
    return signer


def _complete(envelope: Envelope) -> None:
    signers = list(envelope.signers.all())
    original = _storage(envelope.association).get(envelope.source_key)
    output = pdf.signed_pdf(original, pdf.certificate_page(envelope, signers))
    base = (envelope.source_name or "document").rsplit(".", 1)[0]
    stored = _storage(envelope.association).put(
        envelope.association_id,
        f"{base}-signed.pdf",
        output,
        content_type="application/pdf",
        folder="esign",
    )
    envelope.result_key = stored.key
    envelope.result_sha256 = sha256(output)
    envelope.status = Envelope.Status.COMPLETED
    envelope.completed_at = timezone.now()
    envelope.save(update_fields=["result_key", "result_sha256", "status", "completed_at"])
    _event(
        envelope,
        SignatureEvent.Kind.COMPLETED,
        original_sha256=envelope.source_sha256,
        result_sha256=envelope.result_sha256,
        result_key=stored.key,
    )
    audit.record("esign.completed", envelope, changes={"result_sha256": envelope.result_sha256})
    if envelope.on_complete:
        callback = import_string(envelope.on_complete)
        pk = envelope.pk
        transaction.on_commit(lambda: callback(Envelope.all_associations.get(pk=pk)), robust=True)


def needs_review(association):
    """Completed or declined envelopes the board has not yet marked reviewed (Home to-do)."""
    return (
        Envelope.all_associations.filter(
            association=association,
            status__in=(Envelope.Status.COMPLETED, Envelope.Status.DECLINED),
        )
        .exclude(events__kind=SignatureEvent.Kind.REVIEWED)
        .order_by("pk")
    )


def mark_reviewed(envelope: Envelope, *, user=None) -> None:
    """Record that the board has seen the outcome; clears the Home to-do. Idempotent."""
    if envelope.status not in (Envelope.Status.COMPLETED, Envelope.Status.DECLINED):
        raise ESignError(_("Only a completed or declined envelope can be marked reviewed."))
    with transaction.atomic():
        if envelope.events.filter(kind=SignatureEvent.Kind.REVIEWED).exists():
            return
        _event(envelope, SignatureEvent.Kind.REVIEWED, user_id=getattr(user, "pk", None))
        audit.record("esign.reviewed", envelope)
