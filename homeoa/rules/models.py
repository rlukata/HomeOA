"""Yearly law checks: which sections the active rule pack relies on changed since its
law was last checked through. Nothing here changes a rule value."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class LawCheck(AssociationScopedModel):
    class Status(models.TextChoices):
        CHANGES = "changes", _("Changes found")
        NO_CHANGES = "no_changes", _("No changes")
        FAILED = "failed", _("Could not check")
        NO_SOURCE = "no_source", _("No change source")

    run_on = models.DateField(_("run on"))
    pack_id = models.CharField(_("rule pack"), max_length=100)
    pack_version = models.CharField(_("pack version"), max_length=40, blank=True)
    checked_through = models.DateField(_("law checked through"), null=True, blank=True)
    status = models.CharField(_("status"), max_length=20, choices=Status.choices)
    reason = models.TextField(_("reason"), blank=True)
    # [{"section", "changes": [LawChange.to_json()], "places": [Place.to_json()]}]
    findings = models.JSONField(_("findings"), default=list, blank=True)
    # Citations outside the RCW, which no change source checks: [{"citation", "places"}]
    not_checked = models.JSONField(_("not checked automatically"), default=list, blank=True)
    reviewed_at = models.DateTimeField(_("reviewed at"), null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reviewed by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        ordering = ("-run_on", "-pk")
        verbose_name = _("law check")
        verbose_name_plural = _("law checks")

    @property
    def needs_review(self) -> bool:
        return self.status in (self.Status.CHANGES, self.Status.FAILED) and not self.reviewed_at
