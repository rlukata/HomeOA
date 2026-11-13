from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class Notification(AssociationScopedModel):
    """One rendered message of one type to one person. Its deliveries go out per channel."""

    type_key = models.CharField(_("type"), max_length=100)
    recipient = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        related_name="notifications",
        verbose_name=_("recipient"),
    )
    subject = models.CharField(_("subject"), max_length=300, blank=True)
    body = models.TextField(_("body"), blank=True)
    context = models.JSONField(_("context"), default=dict, blank=True, encoder=DjangoJSONEncoder)
    object_key = models.CharField(_("object"), max_length=200, blank=True)
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        verbose_name=_("unit"),
        help_text=_("The unit this message is about; it appears in that unit's comm log."),
    )
    period = models.CharField(_("period"), max_length=50, blank=True)
    idempotency_key = models.CharField(_("idempotency key"), max_length=400)
    as_of = models.DateField(_("as of"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    read_at = models.DateTimeField(_("read at"), null=True, blank=True)
    attachments = models.JSONField(
        _("attachments"),
        default=list,
        blank=True,
        help_text=_("Files sent with the email copy: name, content type and storage key."),
    )

    class Meta:
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "idempotency_key"],
                name="notify_notification_idempotent",
            )
        ]

    def __str__(self) -> str:
        return f"{self.type_key} → {self.recipient}"


class Delivery(AssociationScopedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")
        BOUNCED = "bounced", _("Bounced")
        PREVIEW = "preview", _("Preview only")

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries"
    )
    channel = models.CharField(_("channel"), max_length=50)
    status = models.CharField(_("status"), max_length=20, choices=Status, default=Status.QUEUED)
    provider_id = models.CharField(_("provider id"), max_length=200, blank=True)
    # Our own Message-ID for email, kept when the relay reports its own provider_id, so
    # bounces can match either.
    message_id = models.CharField(_("message id"), max_length=200, blank=True)
    error = models.TextField(_("error"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("delivered at"), null=True, blank=True)
    failed_at = models.DateTimeField(_("failed at"), null=True, blank=True)
    bounced_at = models.DateTimeField(_("bounced at"), null=True, blank=True)
    address = models.CharField(_("address"), max_length=320, blank=True)
    failure_kind = models.CharField(
        _("failure kind"),
        max_length=20,
        blank=True,
        help_text=_(
            "Why a failed delivery failed when it was not the channel, e.g. a missing "
            "attachment. Such failures do not count toward revoking e-notice consent."
        ),
    )
    resolved_at = models.DateTimeField(
        _("handled at"),
        null=True,
        blank=True,
        help_text=_("When the board marked a bounced or failed delivery as handled."),
    )

    class Meta:
        verbose_name = _("delivery")
        verbose_name_plural = _("deliveries")
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "channel"], name="notify_delivery_one_per_channel"
            )
        ]

    def __str__(self) -> str:
        return f"{self.notification} · {self.channel} · {self.status}"


class Preference(AssociationScopedModel):
    """A person's choice for one type on one channel. No row means the type's default."""

    person = models.ForeignKey(
        "registry.Person",
        on_delete=models.CASCADE,
        related_name="notification_preferences",
        verbose_name=_("person"),
    )
    type_key = models.CharField(_("type"), max_length=100)
    channel = models.CharField(_("channel"), max_length=50)
    enabled = models.BooleanField(_("enabled"), default=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("notification preference")
        verbose_name_plural = _("notification preferences")
        constraints = [
            models.UniqueConstraint(
                fields=["person", "type_key", "channel"], name="notify_preference_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.person} · {self.type_key} · {self.channel}: {self.enabled}"
