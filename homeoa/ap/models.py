"""Accounts payable (FIN-12): vendor bills booked to AP when received (accrual).

A bill is entered as a draft, then submitted: submitting posts Dr expense (per line, per
fund) / Cr 2000 Accounts payable (per fund) through ``ledger.services`` and runs the
approval hook. A submitted bill is never edited; voiding posts the reversal and a corrected
bill is entered again. HomeOA never pays a bill: it is paid at the bank, and the
bank feed or a manual "mark paid" records Dr AP / Cr cash.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel


class Bill(AssociationScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING = "pending", _("Pending approval")
        APPROVED = "approved", _("Approved")
        PAID = "paid", _("Paid")
        VOID = "void", _("Void")

    class PaidBy(models.TextChoices):
        BANK = "bank", _("Bank transfer or bill pay")
        CHECK = "check", _("Check")
        CARD = "card", _("Card")
        PAYMENT_APP = "payment_app", _("Payment app (PayPal, Venmo...)")

    #: Paid this way, the processor reports the payment on 1099-K, so it stays out of the
    #: association's 1099-NEC totals (``vendors.ten99``).
    REPORTED_BY_PROCESSOR = (PaidBy.CARD, PaidBy.PAYMENT_APP)

    vendor = models.ForeignKey(
        "vendors.Vendor", on_delete=models.PROTECT, related_name="bills", verbose_name=_("vendor")
    )
    invoice_number = models.CharField(_("invoice number"), max_length=60)
    bill_date = models.DateField(_("bill date"))
    due_date = models.DateField(_("due date"), null=True, blank=True)
    description = models.CharField(_("description"), max_length=255, blank=True)
    status = models.CharField(_("status"), max_length=10, choices=Status, default=Status.DRAFT)

    entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("accrual entry"),
    )
    payment_entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("payment entry"),
    )
    void_entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("void entry"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("entered by"),
    )
    created_at = models.DateTimeField(_("entered at"), auto_now_add=True)
    submitted_at = models.DateTimeField(_("submitted at"), null=True, blank=True)
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    paid_on = models.DateField(_("paid on"), null=True, blank=True)
    paid_by = models.CharField(
        _("paid by"), max_length=12, choices=PaidBy, default=PaidBy.BANK, blank=True
    )
    voided_at = models.DateTimeField(_("voided at"), null=True, blank=True)
    void_reason = models.CharField(_("void reason"), max_length=255, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bill")
        verbose_name_plural = _("bills")
        ordering = ["-bill_date", "-pk"]
        constraints = [
            # A voided bill frees its invoice number so it can be entered again.
            models.UniqueConstraint(
                fields=["association", "vendor", "invoice_number"],
                condition=~Q(status="void"),
                name="ap_bill_invoice_unique_per_vendor",
            )
        ]

    def __str__(self) -> str:
        return f"{self.vendor} #{self.invoice_number}"

    @property
    def total(self):
        return sum((ln.amount for ln in self.lines.all()), ZERO)

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.PENDING, self.Status.APPROVED)

    @property
    def is_editable(self) -> bool:
        return self.status == self.Status.DRAFT

    def is_overdue(self, today) -> bool:
        return self.is_open and self.due_date is not None and self.due_date < today


class BillLine(AssociationScopedModel):
    bill = models.ForeignKey(
        Bill, on_delete=models.CASCADE, related_name="lines", verbose_name=_("bill")
    )
    account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("expense account"),
    )
    fund = models.ForeignKey(
        "ledger.Fund", on_delete=models.PROTECT, related_name="+", verbose_name=_("fund")
    )
    amount = MoneyField(_("amount"))
    memo = models.CharField(_("memo"), max_length=255, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bill line")
        verbose_name_plural = _("bill lines")
        ordering = ["pk"]

    def __str__(self) -> str:
        return f"{self.account} {self.amount}"


class BillAttachment(AssociationScopedModel):
    bill = models.ForeignKey(
        Bill, on_delete=models.CASCADE, related_name="attachments", verbose_name=_("bill")
    )
    file_key = models.CharField(_("file"), max_length=300)
    name = models.CharField(_("file name"), max_length=255)
    content_type = models.CharField(_("content type"), max_length=100)
    size = models.PositiveIntegerField(_("size"), default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("uploaded by"),
    )
    uploaded_at = models.DateTimeField(_("uploaded at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bill attachment")
        verbose_name_plural = _("bill attachments")
        ordering = ["pk"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf"


from .approvals.models import Approval, ApprovalRule  # noqa: E402  (registered under ap)

__all__ = ["Approval", "ApprovalRule", "Bill", "BillAttachment", "BillLine"]
