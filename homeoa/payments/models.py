"""Manual receipts and their allocations to charges (FIN-05).

Write through :mod:`homeoa.payments.services`, never directly. HomeOA never moves money:
a :class:`Receipt` records money that already arrived, a :class:`Refund` records money the
board already sent back outside HomeOA.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class Method(models.TextChoices):
    CHECK = "check", _("Check")
    ZELLE = "zelle", _("Zelle")
    TRANSFER = "transfer", _("Bank transfer")
    ACH = "ach", _("ACH (online)")
    CASH = "cash", _("Cash")
    OTHER = "other", _("Other")


class ReceiptStatus(models.TextChoices):
    POSTED = "posted", _("Posted")
    VOID = "void", _("Void")


class Receipt(AssociationScopedModel):
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("ownership period"),
    )
    payer = models.CharField(_("payer"), max_length=200, blank=True)
    method = models.CharField(_("method"), max_length=12, choices=Method.choices)
    reference = models.CharField(_("reference"), max_length=100, blank=True)
    amount = MoneyField(_("amount"))
    received_date = models.DateField(_("received date"), db_index=True)
    deposit_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("deposit account"),
    )
    fund = models.ForeignKey(
        "ledger.Fund", on_delete=models.PROTECT, related_name="+", verbose_name=_("fund")
    )
    memo = models.CharField(_("memo"), max_length=200, blank=True)
    status = models.CharField(
        _("status"), max_length=8, choices=ReceiptStatus.choices, default=ReceiptStatus.POSTED
    )
    source = models.CharField(_("source"), max_length=20, default="manual")
    entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("ledger entry"),
    )
    void_entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("void entry"),
    )
    void_reason = models.TextField(_("void reason"), blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("recorded by"),
    )
    created_at = models.DateTimeField(_("recorded at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("receipt")
        verbose_name_plural = _("receipts")
        ordering = ["-received_date", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payments_receipt_amount_positive"
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_method_display()} {self.amount}"


class Allocation(AssociationScopedModel):
    """``amount`` of one credit source (a receipt or a billing adjustment) applied to a charge.

    ``entry`` is the ledger entry that moved it (the receipt's own entry, an adjustment's
    entry, or a Dr 2100 / Cr 1100 credit application). A released allocation (receipt void,
    charge void) keeps its row with ``released_entry`` set and no longer counts.
    """

    charge = models.ForeignKey(
        "billing.Charge",
        on_delete=models.PROTECT,
        related_name="allocations",
        verbose_name=_("charge"),
    )
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="allocations",
        verbose_name=_("receipt"),
    )
    adjustment = models.ForeignKey(
        "billing.Adjustment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="allocations",
        verbose_name=_("adjustment"),
    )
    amount = MoneyField(_("amount"))
    entry = models.ForeignKey("ledger.JournalEntry", on_delete=models.PROTECT, related_name="+")
    released_entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("allocation")
        verbose_name_plural = _("allocations")
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payments_allocation_amount_positive"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(receipt__isnull=False, adjustment__isnull=True)
                    | models.Q(receipt__isnull=True, adjustment__isnull=False)
                ),
                name="payments_allocation_one_source",
            ),
        ]


class Refund(AssociationScopedModel):
    """A refund the board issued outside HomeOA; HomeOA only records it.

    Posts Dr 2100 prepaid (tagged) / Cr cash, consuming unapplied credit of ``receipt``.
    """

    receipt = models.ForeignKey(
        Receipt, on_delete=models.PROTECT, related_name="refunds", verbose_name=_("receipt")
    )
    amount = MoneyField(_("amount"))
    date = models.DateField(_("date"))
    method = models.CharField(_("method"), max_length=12, choices=Method.choices)
    reference = models.CharField(_("reference"), max_length=100, blank=True)
    reason = models.TextField(_("reason"))
    entry = models.OneToOneField("ledger.JournalEntry", on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("refund")
        verbose_name_plural = _("refunds")
        ordering = ["-date", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payments_refund_amount_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""), name="payments_refund_reason_required"
            ),
        ]


# ------------------------------------------------------------------ autopay


class AutopayRule(models.TextChoices):
    BALANCE = "balance", _("Statement balance")
    FIXED = "fixed", _("Fixed amount")


class AutopayEnrollment(AssociationScopedModel):
    """An owner's autopay for one ownership period. See ``payments.autopay``.

    ``mandate_ref`` is the payment provider's reference for the authorized bank account
    (``SavedMethod.ref``); the provider checks it at every debit.
    """

    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("ownership period"),
    )
    person = models.ForeignKey(
        "registry.Person", on_delete=models.PROTECT, related_name="+", verbose_name=_("owner")
    )
    rule = models.CharField(_("amount"), max_length=10, choices=AutopayRule.choices)
    fixed_amount = MoneyField(_("fixed amount"), null=True, blank=True)
    mandate_ref = models.CharField(_("bank authorization"), max_length=64)
    mandate_label = models.CharField(_("bank account"), max_length=120, blank=True)
    starts_on = models.DateField(_("starts on"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(_("created"), auto_now_add=True)
    cancelled_at = models.DateTimeField(_("turned off"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("autopay enrollment")
        verbose_name_plural = _("autopay enrollments")
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["ownership_period"],
                condition=models.Q(cancelled_at__isnull=True),
                name="payments_autopay_one_active_per_period",
            ),
            models.CheckConstraint(
                condition=~models.Q(rule="fixed")
                | models.Q(fixed_amount__isnull=False, fixed_amount__gt=0),
                name="payments_autopay_fixed_amount_positive",
            ),
        ]

    def __str__(self):
        return _("Autopay · Unit %(unit)s") % {"unit": self.unit.number}

    @property
    def active(self) -> bool:
        return self.cancelled_at is None


class AutopayStatus(models.TextChoices):
    SCHEDULED = "scheduled", _("Scheduled")
    PENDING = "pending", _("Processing")
    FAILED = "failed", _("Failed")
    SKIPPED = "skipped", _("Nothing owed")
    SKIPPED_NOTICE_FAILED = "skipped_notice_failed", _("Not debited: notice failed")


class NoticeSource(models.TextChoices):
    NOTICE = "notice", _("Pre-debit notice")
    STATEMENT = "statement", _("Account statement")
    NONE = "none", _("Not needed (fixed amount)")


class AutopayCharge(AssociationScopedModel):
    """One autopay attempt per unit and month (the idempotency record).

    Created ``scheduled`` on the notice date with what the owner was told:
    ``noticed_amount`` (the most the debit may take), ``noticed_on`` and ``debit_on``, and
    which notice served (``notice_source``; ``notice_statement`` when a sent statement did).
    On ``debit_on`` it becomes ``pending``, ``failed`` or ``skipped`` and ``amount`` is the
    amount actually debited. When the pre-debit notice could not be sent it is
    ``skipped_notice_failed`` at once and is never debited.
    """

    enrollment = models.ForeignKey(
        AutopayEnrollment,
        on_delete=models.PROTECT,
        related_name="charges",
        verbose_name=_("enrollment"),
    )
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    period = models.DateField(_("month"))
    amount = MoneyField(_("amount"))
    status = models.CharField(_("status"), max_length=30, choices=AutopayStatus.choices)
    reference = models.CharField(_("reference"), max_length=100, blank=True)
    message = models.CharField(_("message"), max_length=500, blank=True)
    failed_at = models.DateTimeField(_("failed at"), null=True, blank=True)
    acknowledged_at = models.DateTimeField(_("acknowledged at"), null=True, blank=True)
    noticed_amount = MoneyField(_("noticed amount"), null=True, blank=True)
    noticed_on = models.DateField(_("noticed on"), null=True, blank=True)
    debit_on = models.DateField(_("debit date"), null=True, blank=True)
    notice_source = models.CharField(
        _("notice"), max_length=10, choices=NoticeSource.choices, blank=True
    )
    notice_statement = models.ForeignKey(
        "statements.Statement",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("statement used as notice"),
    )
    notice_resends = models.PositiveSmallIntegerField(_("notice resends"), default=0)
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("autopay charge")
        verbose_name_plural = _("autopay charges")
        ordering = ["-period", "-pk"]
        constraints = [
            # One live debit per unit and month; failed attempts stay as history.
            models.UniqueConstraint(
                fields=["unit", "period"],
                condition=~models.Q(status="failed"),
                name="payments_autopay_unit_month",
            )
        ]

    def __str__(self):
        return _("Autopay · Unit %(unit)s · %(month)s") % {
            "unit": self.unit.number,
            "month": self.period.strftime("%Y-%m"),
        }


class AutopayRun(AssociationScopedModel):
    """A day's autopay job that started at least one debit (the activity feed line)."""

    as_of = models.DateField(_("date"))
    units = models.PositiveIntegerField(_("units"))
    total = MoneyField(_("total"))
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("autopay run")
        verbose_name_plural = _("autopay runs")
        ordering = ["-as_of", "-pk"]

    def __str__(self):
        return ngettext(
            "Autopay collected from %(n)d unit",
            "Autopay collected from %(n)d units",
            self.units,
        ) % {"n": self.units}
