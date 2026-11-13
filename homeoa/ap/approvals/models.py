"""Bill approval rules and recorded decisions (FIN-12)."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class ApprovalRule(AssociationScopedModel):
    """How many approvals a bill needs. The first active rule (lowest ``priority``) whose
    amount range, category and vendor all match the bill applies."""

    name = models.CharField(_("name"), max_length=100)
    priority = models.PositiveIntegerField(_("priority"), default=100)
    active = models.BooleanField(_("active"), default=True)
    min_amount = MoneyField(_("from amount (inclusive)"), default=Decimal("0.00"))
    max_amount = MoneyField(_("up to amount (exclusive)"), null=True, blank=True)
    category = models.CharField(
        _("vendor category"),
        max_length=100,
        blank=True,
        help_text=_("Leave blank to match any category."),
    )
    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("vendor"),
    )
    required_approvals = models.PositiveSmallIntegerField(
        _("approvals required"),
        default=1,
        help_text=_("0 approves the bill as soon as it is submitted."),
    )
    eligible_roles = models.JSONField(
        _("eligible roles"),
        default=list,
        blank=True,
        help_text=_("Roles that may approve. Leave empty for board members."),
    )
    allow_submitter = models.BooleanField(
        _("the person who entered the bill may approve it"), default=False
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("approval rule")
        verbose_name_plural = _("approval rules")
        ordering = ["priority", "pk"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        if self.min_amount is not None and self.min_amount < 0:
            raise ValidationError({"min_amount": _("Amounts cannot be negative.")})
        if (
            self.max_amount is not None
            and self.min_amount is not None
            and self.max_amount <= self.min_amount
        ):
            raise ValidationError(
                {"max_amount": _("The upper amount must be above the lower amount.")}
            )

    @property
    def roles(self) -> frozenset[str]:
        return frozenset(self.eligible_roles or ()) or frozenset({perms.BOARD})


class Approval(AssociationScopedModel):
    """One person's decision on one submission of a bill. ``entry`` is the accrual entry
    of that submission, so a reject and resubmit starts a fresh round."""

    class Decision(models.TextChoices):
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    bill = models.ForeignKey(
        "ap.Bill", on_delete=models.CASCADE, related_name="approvals", verbose_name=_("bill")
    )
    entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("accrual entry"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("approver"),
    )
    decision = models.CharField(_("decision"), max_length=10, choices=Decision)
    rule = models.ForeignKey(
        ApprovalRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("rule"),
    )
    reason = models.CharField(_("reason"), max_length=255, blank=True)
    created_at = models.DateTimeField(_("decided at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("bill approval")
        verbose_name_plural = _("bill approvals")
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["bill", "entry", "user"], name="ap_approval_one_per_person_per_round"
            )
        ]

    def __str__(self) -> str:
        return f"{self.bill} {self.decision} {self.user}"
