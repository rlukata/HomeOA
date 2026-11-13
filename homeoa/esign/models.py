"""Envelopes, signers and the append-only signature event trail (GOV-08)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class Envelope(AssociationScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Out for signature")
        COMPLETED = "completed", _("Completed")
        DECLINED = "declined", _("Declined")
        VOIDED = "voided", _("Voided")

    class Routing(models.TextChoices):
        SEQUENTIAL = "sequential", _("In order")
        PARALLEL = "parallel", _("All at once")

    title = models.CharField(_("title"), max_length=200)
    message = models.TextField(_("message"), blank=True)
    source_key = models.CharField(_("source file"), max_length=500)
    source_name = models.CharField(_("file name"), max_length=200, blank=True)
    #: SHA-256 of the source file when the envelope was sent; re-checked before each signature.
    source_sha256 = models.CharField(max_length=64, blank=True)
    routing = models.CharField(
        _("signing order"), max_length=12, choices=Routing.choices, default=Routing.SEQUENTIAL
    )
    status = models.CharField(
        _("status"), max_length=12, choices=Status.choices, default=Status.DRAFT
    )
    result_key = models.CharField(max_length=500, blank=True)
    result_sha256 = models.CharField(max_length=64, blank=True)
    #: Dotted path of ``callable(envelope)`` run on completion (e.g. minutes filing the PDF
    #: with ``documents.attach``), plus a free reference for it.
    on_complete = models.CharField(max_length=200, blank=True)
    on_complete_ref = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return self.title

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.DRAFT, self.Status.SENT)


class Signer(AssociationScopedModel):
    class Status(models.TextChoices):
        WAITING = "waiting", _("Waiting")
        INVITED = "invited", _("Invited")
        SIGNED = "signed", _("Signed")
        DECLINED = "declined", _("Declined")

    envelope = models.ForeignKey(Envelope, on_delete=models.CASCADE, related_name="signers")
    order = models.PositiveIntegerField(_("order"), default=1)
    name = models.CharField(_("name"), max_length=200)
    email = models.EmailField(_("email"))
    #: When set, the signer must be logged in as this user to sign.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WAITING)
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    token_used_at = models.DateTimeField(null=True, blank=True)
    signed_name = models.CharField(max_length=200, blank=True)
    acted_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    decline_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"


class EventImmutableError(RuntimeError):
    pass


class SignatureEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise EventImmutableError("signature events are append-only")

    def delete(self):
        raise EventImmutableError("signature events are append-only")


class SignatureEvent(AssociationScopedModel):
    """One step in an envelope's history. Rows are never changed or deleted."""

    class Kind(models.TextChoices):
        CREATED = "created", _("Created")
        SENT = "sent", _("Sent")
        INVITED = "invited", _("Invitation emailed")
        VIEWED = "viewed", _("Viewed")
        CONSENTED = "consented", _("Consented to sign electronically")
        SIGNED = "signed", _("Signed")
        DECLINED = "declined", _("Declined")
        VOIDED = "voided", _("Voided")
        TAMPER = "tamper", _("Source file changed; signing refused")
        COMPLETED = "completed", _("Completed")
        INVITE_FAILED = "invite_failed", _("Invitation email failed")
        REVIEWED = "reviewed", _("Outcome reviewed by the board")

    envelope = models.ForeignKey(Envelope, on_delete=models.PROTECT, related_name="events")
    signer = models.ForeignKey(
        Signer, null=True, blank=True, on_delete=models.PROTECT, related_name="events"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    at = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    data = models.JSONField(default=dict, blank=True)

    objects = SignatureEventQuerySet.as_manager()
    all_associations = SignatureEventQuerySet.as_manager()

    class Meta:
        ordering = ["at", "pk"]
        base_manager_name = "all_associations"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise EventImmutableError("signature events are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise EventImmutableError("signature events are append-only")
