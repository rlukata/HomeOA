"""Period close (FIN-15): month closes and year-end closes.

A close locks the ledger through ``period_end`` (``ledger.services.set_locked_through``).
The lock is always the latest ``period_end`` among closes that are not reopened.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel


class CloseKind(models.TextChoices):
    MONTH = "month", _("Month")
    YEAR = "year", _("Year end")


class Close(AssociationScopedModel):
    kind = models.CharField(_("kind"), max_length=8, choices=CloseKind)
    period_start = models.DateField(_("period start"))
    period_end = models.DateField(_("period end"))
    closed_at = models.DateTimeField(_("closed at"), auto_now_add=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("closed by"),
    )
    entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("closing entry"),
    )
    reopened_at = models.DateTimeField(_("reopened at"), null=True, blank=True)
    reopened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reopened by"),
    )
    reopen_reason = models.TextField(_("reason for reopening"), blank=True)
    reversal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reversal of closing entry"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("close")
        verbose_name_plural = _("closes")
        ordering = ["-period_end", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "kind", "period_end"],
                condition=models.Q(reopened_at__isnull=True),
                name="closing_one_open_close_per_period",
            )
        ]

    def __str__(self):
        return (
            f"{self.get_kind_display()} {self.period_start:%Y-%m-%d} – {self.period_end:%Y-%m-%d}"
        )

    @property
    def is_active(self) -> bool:
        return self.reopened_at is None
