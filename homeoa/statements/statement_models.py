"""Owner statements (FIN-04): one run per month, one statement per unit ownership period."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.models import AssociationScopedModel

ZERO = Decimal("0.00")


def _money(name):
    return models.DecimalField(name, max_digits=12, decimal_places=2, default=ZERO)


class StatementRun(AssociationScopedModel):
    """Statements for one calendar month. Re-running replaces the unsent statements."""

    period_start = models.DateField(_("month"))
    period_end = models.DateField(_("month end"))
    generated_at = models.DateTimeField(_("generated at"), null=True, blank=True)
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = _("statement run")
        verbose_name_plural = _("statement runs")
        ordering = ["-period_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "period_start"], name="statements_run_one_per_month"
            )
        ]

    def __str__(self) -> str:
        return self.period_start.strftime("%Y-%m")


class Statement(AssociationScopedModel):
    """One unit, one ownership period, one month: opening, charges, payments, closing."""

    class Channel(models.TextChoices):
        EMAIL = "email", _("Email")
        MAIL = "mail", _("Mail")

    class Status(models.TextChoices):
        PREVIEW = "preview", _("Preview only")
        SENT = "sent", _("Sent")

    run = models.ForeignKey(StatementRun, on_delete=models.CASCADE, related_name="statements")
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="statements", verbose_name=_("unit")
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="statements",
        verbose_name=_("ownership period"),
    )
    period_start = models.DateField(_("from"))
    period_end = models.DateField(_("through"))
    owner_names = models.CharField(_("owners"), max_length=300, blank=True)
    opening = _money(_("opening balance"))
    charges = _money(_("charges"))
    payments = _money(_("payments and credits"))
    closing = _money(_("closing balance"))
    channel = models.CharField(_("channel"), max_length=10, choices=Channel)
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.PREVIEW)
    letter = models.ForeignKey(
        "letters.Letter", on_delete=models.PROTECT, related_name="+", verbose_name=_("PDF")
    )
    mail_batch = models.ForeignKey(
        "letters.MailBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("mail batch"),
    )
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    # The next autopay debit printed on the statement: for a balance-rule
    # enrollment the projected amount, which lets a statement sent early enough serve as the
    # pre-debit notice; for a fixed amount, informational only.
    autopay_debit_on = models.DateField(_("autopay debit date"), null=True, blank=True)
    autopay_amount = models.DecimalField(
        _("autopay amount"), max_digits=12, decimal_places=2, null=True, blank=True
    )
    autopay_fixed = models.BooleanField(_("fixed autopay amount"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("statement")
        verbose_name_plural = _("statements")
        ordering = ["run", "unit__number", "period_start"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "ownership_period"], name="statements_one_per_owner_period"
            )
        ]

    def __str__(self) -> str:
        return f"{self.unit} · {self.period_start}–{self.period_end}"
