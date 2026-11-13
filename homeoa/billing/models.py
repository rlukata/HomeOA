"""Assessment schedules, billing runs and charges (FIN-03).

A :class:`Charge` is the A/R line other features allocate payments against. Every
charge has exactly one ledger entry (``entry``) posted through ``ledger.services.post_entry``.
Write through :mod:`homeoa.billing.services`, never directly.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel


class Frequency(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")


class Method(models.TextChoices):
    ALLOCATED_INTEREST = "allocated_interest", _("By allocated interest")
    FLAT = "flat", _("Flat (equal per unit)")
    UNIT_TYPE = "unit_type", _("By unit type")


class AssessmentSchedule(AssociationScopedModel):
    """How regular assessments are computed and posted.

    The amount is the association total per year (``annual_amount``) or per period
    (``period_amount``); for ``unit_type`` it is ``type_amounts`` (per-unit amount per
    period for each type) with ``unit_types`` mapping unit numbers to a type.
    Owners are billed entirely in the operating fund. ``reserve_annual_amount`` is the
    budgeted reserve contribution for the year; each billing run moves one month of it from
    operating to reserve with one interfund entry (see :mod:`homeoa.billing.services`).
    ``insurance_annual_amount`` does the same for the insurance fund.
    """

    name = models.CharField(_("name"), max_length=100)
    frequency = models.CharField(
        _("frequency"), max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY
    )
    method = models.CharField(
        _("method"), max_length=20, choices=Method.choices, default=Method.ALLOCATED_INTEREST
    )
    annual_amount = MoneyField(_("annual amount"), null=True, blank=True)
    period_amount = MoneyField(_("amount per month"), null=True, blank=True)
    type_amounts = models.JSONField(
        _("amount per unit type"),
        default=dict,
        blank=True,
        help_text=_('Per-unit amount each month, e.g. {"1BR": "300.00"}.'),
    )
    unit_types = models.JSONField(
        _("unit types"),
        default=dict,
        blank=True,
        help_text=_('Unit number to type, e.g. {"1": "1BR"}.'),
    )
    reserve_annual_amount = MoneyField(
        _("reserve contribution per year"),
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text=_("The budgeted transfer to the reserve fund; one twelfth posts each month."),
    )
    insurance_annual_amount = MoneyField(
        _("insurance transfer per year"),
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text=_("The budgeted transfer to the insurance fund; one twelfth posts each month."),
    )
    receivable_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("receivable account"),
    )
    income_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("income account"),
    )
    operating_fund = models.ForeignKey(
        "ledger.Fund",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("operating fund"),
    )
    reserve_fund = models.ForeignKey(
        "ledger.Fund",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("reserve fund"),
    )
    reserve_expense_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("reserve contribution expense (operating)"),
    )
    due_to_reserve_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("due to reserve (operating)"),
    )
    due_from_operating_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("due from operating (reserve)"),
    )
    reserve_income_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("reserve contribution income (reserve)"),
    )
    # Insurance transfer accounts; required only when insurance_annual_amount > 0.
    insurance_fund = models.ForeignKey(
        "ledger.Fund",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("insurance fund"),
    )
    insurance_expense_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("transfer to insurance expense (operating)"),
    )
    due_to_insurance_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("due to insurance (operating)"),
    )
    insurance_due_from_operating_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("due from operating (insurance)"),
    )
    insurance_income_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("insurance transfer income (insurance)"),
    )
    post_day = models.PositiveSmallIntegerField(
        _("posting day"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(28)],
        help_text=_("Day of the month charges post automatically."),
    )
    due_day = models.PositiveSmallIntegerField(
        _("due day"), default=1, validators=[MinValueValidator(1), MaxValueValidator(28)]
    )
    effective_start = models.DateField(_("effective from"))
    effective_end = models.DateField(_("effective until"), null=True, blank=True)
    active = models.BooleanField(_("active"), default=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("assessment schedule")
        verbose_name_plural = _("assessment schedules")
        ordering = ["-effective_start", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reserve_annual_amount__gte=0),
                name="billing_schedule_reserve_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(insurance_annual_amount__gte=0),
                name="billing_schedule_insurance_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        needs_total = self.method in (Method.ALLOCATED_INTEREST, Method.FLAT)
        if needs_total and (self.annual_amount is None) == (self.period_amount is None):
            raise ValidationError(_("Give an annual amount or an amount per month, not both."))
        if self.method == Method.UNIT_TYPE and not self.type_amounts:
            raise ValidationError(_("Give an amount for each unit type."))
        if (
            self.effective_end
            and self.effective_start
            and self.effective_end < self.effective_start
        ):
            raise ValidationError(_("The end date is before the start date."))
        insurance_fields = (
            self.insurance_fund_id,
            self.insurance_expense_account_id,
            self.due_to_insurance_account_id,
            self.insurance_due_from_operating_account_id,
            self.insurance_income_account_id,
        )
        if self.insurance_annual_amount and not all(insurance_fields):
            raise ValidationError(_("Choose the insurance fund and its transfer accounts."))

    def covers(self, period) -> bool:
        """Whether the month starting ``period`` falls in the effective dates."""
        if period < self.effective_start.replace(day=1):
            return False
        return self.effective_end is None or period <= self.effective_end


class BillingRun(AssociationScopedModel):
    """One posted month of one schedule. Unique per schedule and period: the idempotency key."""

    schedule = models.ForeignKey(
        AssessmentSchedule,
        on_delete=models.PROTECT,
        related_name="runs",
        verbose_name=_("schedule"),
    )
    period = models.DateField(_("period"), help_text=_("First day of the billed month."))
    charge_date = models.DateField(_("charge date"))
    posted_at = models.DateTimeField(_("posted at"), auto_now_add=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("posted by"),
    )
    source = models.CharField(_("source"), max_length=20, default="manual")
    total = MoneyField(_("total"), default=ZERO)
    charge_count = models.PositiveIntegerField(_("charges"), default=0)
    reserve_amount = MoneyField(_("reserve contribution"), default=ZERO)
    reserve_entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reserve transfer entry"),
    )
    insurance_amount = MoneyField(_("insurance transfer"), default=ZERO)
    insurance_entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("insurance transfer entry"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("billing run")
        verbose_name_plural = _("billing runs")
        ordering = ["-period", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["schedule", "period"], name="billing_run_unique_schedule_period"
            )
        ]

    def __str__(self) -> str:
        return f"{self.schedule} {self.period:%Y-%m}"


class ChargeKind(models.TextChoices):
    ASSESSMENT = "assessment", _("Assessment")
    LATE_FEE = "late_fee", _("Late fee")
    INTEREST = "interest", _("Interest")
    OTHER = "other", _("Other charge")
    SPECIAL = "special", _("Special assessment")
    FINE = "fine", _("Fine")


class Charge(AssociationScopedModel):
    """One amount billed to a unit's owner of record on ``charge_date``."""

    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("ownership period"),
    )
    kind = models.CharField(_("type"), max_length=20, choices=ChargeKind.choices)
    description = models.CharField(_("description"), max_length=200)
    amount = MoneyField(_("amount"))
    open_balance = MoneyField(_("open balance"))
    charge_date = models.DateField(_("charge date"), db_index=True)
    due_date = models.DateField(_("due date"))
    period = models.DateField(_("period"), null=True, blank=True)
    run = models.ForeignKey(
        BillingRun,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="charges",
        verbose_name=_("billing run"),
    )
    entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
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

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("charge")
        verbose_name_plural = _("charges")
        ordering = ["charge_date", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["run", "unit"], name="billing_charge_unique_run_unit"),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="billing_charge_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.description


class AdjustmentKind(models.TextChoices):
    CREDIT_MEMO = "credit_memo", _("Credit memo")
    WRITE_OFF = "write_off", _("Write-off")
    MANUAL_CHARGE = "manual_charge", _("Manual charge")
    CHARGE_VOID = "charge_void", _("Charge voided")


class Adjustment(AssociationScopedModel):
    """A board change to a unit's balance, always with a reason (FIN-11).

    Write through :mod:`homeoa.billing.services.adjustments`. A credit memo or write-off is
    a credit *source* that payments' allocation engine applies to charges, like a receipt.
    """

    kind = models.CharField(_("type"), max_length=20, choices=AdjustmentKind.choices)
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("ownership period"),
    )
    amount = MoneyField(_("amount"))
    date = models.DateField(_("date"))
    reason = models.TextField(_("reason"))
    account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("offset account"),
    )
    charge = models.ForeignKey(
        Charge,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="adjustments",
        verbose_name=_("charge"),
    )
    entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("ledger entry"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("adjustment")
        verbose_name_plural = _("adjustments")
        ordering = ["-date", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="billing_adjustment_amount_positive"
            ),
            models.CheckConstraint(
                condition=~models.Q(reason=""), name="billing_adjustment_reason_required"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount}"


# Late fees, interest and special assessments keep their models in their own module.
from .latefee_models import (  # noqa: E402, F401
    LateFee,
    LateFeePolicy,
    SpecialAssessment,
    SpecialInstallment,
)
