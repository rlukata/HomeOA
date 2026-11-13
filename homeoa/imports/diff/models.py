"""Parallel-run diff results (FIN-18). Read-only against the books: nothing here posts."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class DiffRun(AssociationScopedModel):
    """One comparison of a stored AppFolio snapshot against HomeOA's books as of its date."""

    snapshot = models.ForeignKey(
        "imports.ImportRun",
        on_delete=models.PROTECT,
        related_name="diff_runs",
        verbose_name=_("Management company snapshot"),
    )
    as_of = models.DateField(_("as of"))
    period_start = models.DateField(_("period start"))
    warnings = models.JSONField(_("warnings"), default=list, blank=True)
    units_matched = models.PositiveIntegerField(_("units matched"), default=0)
    accounts_matched = models.PositiveIntegerField(_("accounts matched"), default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("run by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("parallel-run check")
        verbose_name_plural = _("parallel-run checks")
        ordering = ["-as_of", "-pk"]

    def __str__(self) -> str:
        return str(_("Parallel run %(d)s") % {"d": self.as_of.isoformat()})

    def open_differences(self):
        return self.lines.filter(counted=True, explained_at__isnull=True)

    @property
    def is_clean(self) -> bool:
        return not self.open_differences().exists()

    @property
    def status_label(self) -> str:
        return str(_("Clean") if self.is_clean else _("Differences"))


class DiffLine(AssociationScopedModel):
    class Kind(models.TextChoices):
        UNIT = "unit", _("Owner balance")
        ACCOUNT = "account", _("GL account")

    run = models.ForeignKey(
        DiffRun, on_delete=models.CASCADE, related_name="lines", verbose_name=_("check")
    )
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices)
    key = models.CharField(_("unit or account"), max_length=40)
    label = models.CharField(_("label"), max_length=200, blank=True)
    homeoa = models.DecimalField(_("HomeOA"), max_digits=14, decimal_places=2)
    appfolio = models.DecimalField(_("Management company"), max_digits=14, decimal_places=2)
    difference = models.DecimalField(_("difference"), max_digits=14, decimal_places=2)
    # False when the difference is already shown elsewhere (a receivable/prepaid account whose
    # difference equals the owner-balance differences), so it is not counted twice.
    counted = models.BooleanField(_("counted"), default=True)
    note = models.TextField(_("explanation"), blank=True)
    explained_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("explained by"),
    )
    explained_at = models.DateTimeField(_("explained at"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("parallel-run difference")
        verbose_name_plural = _("parallel-run differences")
        ordering = ["kind", "pk"]

    def __str__(self) -> str:
        return f"{self.key}: {self.difference}"

    @property
    def explained(self) -> bool:
        return self.explained_at is not None
