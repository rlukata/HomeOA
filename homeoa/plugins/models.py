from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeoa.core.crypto import EncryptedJSONField
from homeoa.core.models import AssociationScopedModel


class PluginInstallation(AssociationScopedModel):
    """Per-association enablement and configuration of a plugin (PLG-02, PLG-05)."""

    plugin_id = models.CharField(_("plugin"), max_length=50)
    enabled = models.BooleanField(_("enabled"), default=False)
    settings = models.JSONField(_("settings"), default=dict, blank=True)
    secrets = EncryptedJSONField(_("secrets"), default=dict, blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("plugin installation")
        verbose_name_plural = _("plugin installations")
        ordering = ["plugin_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "plugin_id"], name="plugininstallation_unique_per_assoc"
            )
        ]

    def __str__(self) -> str:
        return self.plugin_id


class WebhookDelivery(AssociationScopedModel):
    """One processed inbound webhook; the unique event id makes replays no-ops (PLG-06)."""

    plugin_id = models.CharField(_("plugin"), max_length=50)
    event_id = models.CharField(_("event id"), max_length=255)
    received_at = models.DateTimeField(_("received at"), auto_now_add=True)

    class Meta:
        verbose_name = _("webhook delivery")
        verbose_name_plural = _("webhook deliveries")
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "plugin_id", "event_id"],
                name="webhookdelivery_unique_event",
            )
        ]

    def __str__(self) -> str:
        return f"{self.plugin_id}:{self.event_id}"


class ModuleSwitch(AssociationScopedModel):
    """Whether the board has turned an optional module on for an association.

    No row means the module is off. Plugins keep their own switch in
    ``PluginInstallation``; this table holds only the board's feature switches.
    """

    module_id = models.CharField(_("module"), max_length=50)
    enabled = models.BooleanField(_("enabled"), default=False)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("changed by"),
    )
    changed_at = models.DateTimeField(_("changed at"), default=timezone.now)
    change_request = models.ForeignKey(
        "changes.ChangeRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("change request"),
    )

    class Meta:
        verbose_name = _("module switch")
        verbose_name_plural = _("module switches")
        ordering = ["module_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "module_id"], name="moduleswitch_unique_per_assoc"
            )
        ]

    def __str__(self) -> str:
        return f"{self.module_id}: {'on' if self.enabled else 'off'}"
