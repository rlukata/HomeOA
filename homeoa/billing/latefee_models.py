"""Late fees, interest and special assessments (FIN-09, FIN-10).

Write through :mod:`homeoa.billing.services.latefees` and
:mod:`homeoa.billing.services.special`, never directly.
Imported by :mod:`homeoa.billing.models` so Django registers them.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel


def default_applies_to():
    return ["assessment", "special"]


class LateFeePolicy(AssociationScopedModel):
    """The association's late fee and interest terms (placeholders until adopted).

    A late fee is ``flat_amount`` plus ``percent`` of the unpaid balance, assessed once per
    charge once ``grace_days`` have passed since the due date, then limited to the rule
    pack's ``late_fees.cap`` when one is set. Interest at ``interest_apr`` accrues monthly
    on the unpaid balance of overdue charges. Only charges due on or after
    ``effective_start`` and of a kind in ``applies_to`` are considered.
    """

    name = models.CharField(_("name"), max_length=100)
    active = models.BooleanField(_("active"), default=True)
    grace_days = models.PositiveSmallIntegerField(
        _("grace period (days)"),
        default=10,
        help_text=_("Days after the due date before a fee is charged."),
    )
    flat_amount = MoneyField(_("flat fee"), default=ZERO, validators=[MinValueValidator(ZERO)])
    percent = models.DecimalField(
        _("percent of unpaid balance"),
        max_digits=6,
        decimal_places=3,
        default=ZERO,
        validators=[MinValueValidator(ZERO), MaxValueValidator(100)],
    )
    interest_apr = models.DecimalField(
        _("interest (% a year)"),
        max_digits=6,
        decimal_places=3,
        default=ZERO,
        validators=[MinValueValidator(ZERO), MaxValueValidator(100)],
        help_text=_("Simple interest on overdue balances, posted monthly. 0 for none."),
    )
    # Where the law charges its maximum rate unless the association sets one (rule key
    # ``interest.statutory_default_applies``), the board records the resolution adopting
    # ``interest_apr`` (it may be 0). Home asks for it until then.
    interest_adopted_on = models.DateField(
        _("interest rate adopted on"),
        null=True,
        blank=True,
        help_text=_("The date the board adopted this interest rate (0% counts)."),
    )
    interest_resolution = models.CharField(
        _("adopted by"),
        max_length=200,
        blank=True,
        help_text=_("For example: Board resolution 2026-07, minutes of Nov 12, 2026."),
    )
    minimum_balance = MoneyField(
        _("minimum unpaid balance"),
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
        help_text=_("No fee or interest when the unpaid amount is at or below this."),
    )
    applies_to = models.JSONField(
        _("applies to charge types"),
        default=default_applies_to,
        help_text=_('Charge types, e.g. ["assessment", "special"].'),
    )
    effective_start = models.DateField(
        _("effective from"), help_text=_("Only charges due on or after this date.")
    )
    receivable_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("receivable account"),
    )
    fee_income_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("late fee income account"),
    )
    interest_income_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("interest income account"),
    )
    fund = models.ForeignKey(
        "ledger.Fund", on_delete=models.PROTECT, related_name="+", verbose_name=_("fund")
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("late fee policy")
        verbose_name_plural = _("late fee policies")
        ordering = ["-effective_start", "pk"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        from .models import ChargeKind

        kinds = self.applies_to if isinstance(self.applies_to, list) else None
        if kinds is not None and ChargeKind.FINE in kinds:
            # Fines never accrue late fees or interest.
            raise ValidationError({"applies_to": _("Fines never accrue late fees or interest.")})
        if kinds is None or any(k not in ChargeKind.values for k in kinds):
            raise ValidationError(
                {
                    "applies_to": _("List charge types from: %(k)s.")
                    % {"k": ", ".join(ChargeKind.values)}
                }
            )


class LateFeeKind(models.TextChoices):
    LATE_FEE = "late_fee", _("Late fee")
    INTEREST = "interest", _("Interest")


class LateFee(AssociationScopedModel):
    """One fee or month of interest assessed on one charge. The idempotency key is
    (policy, charge, kind, period): ``period`` is the due date for a late fee and the first
    day of the accrued month for interest."""

    policy = models.ForeignKey(
        LateFeePolicy, on_delete=models.PROTECT, related_name="fees", verbose_name=_("policy")
    )
    source_charge = models.ForeignKey(
        "billing.Charge",
        on_delete=models.PROTECT,
        related_name="late_fees",
        verbose_name=_("late charge"),
    )
    kind = models.CharField(_("type"), max_length=12, choices=LateFeeKind.choices)
    period = models.DateField(_("period"))
    charge = models.OneToOneField(
        "billing.Charge",
        on_delete=models.PROTECT,
        related_name="late_fee_record",
        verbose_name=_("fee charge"),
    )
    computed = MoneyField(_("computed amount"))
    capped = models.BooleanField(_("limited by cap"), default=False)
    waived_at = models.DateTimeField(_("waived at"), null=True, blank=True)
    waived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("waived by"),
    )
    waive_reason = models.CharField(_("waiver reason"), max_length=300, blank=True)
    waive_entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("waiver entry"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("late fee")
        verbose_name_plural = _("late fees")
        ordering = ["-period", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "source_charge", "kind", "period"],
                name="billing_latefee_unique_once",
            )
        ]

    def __str__(self) -> str:
        return str(self.charge)

    @property
    def waived(self) -> bool:
        return self.waived_at is not None


class SpecialMethod(models.TextChoices):
    ALLOCATED_INTEREST = "allocated_interest", _("By allocated interest")
    FLAT = "flat", _("Flat (equal per unit)")


class SpecialAssessment(AssociationScopedModel):
    """A one-time or installment special assessment. ``total`` is split into installments
    (equal, leftover cents on the first ones) and each installment across units."""

    name = models.CharField(_("name"), max_length=100)
    description = models.TextField(_("description"), blank=True)
    total = MoneyField(_("total"), validators=[MinValueValidator(ZERO)])
    method = models.CharField(
        _("method"),
        max_length=20,
        choices=SpecialMethod.choices,
        default=SpecialMethod.ALLOCATED_INTEREST,
    )
    operating_fund = models.ForeignKey(
        "ledger.Fund",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("operating fund"),
        help_text=_("Owners are billed in the operating fund."),
    )
    for_reserves = models.BooleanField(
        _("designated for reserves"),
        default=False,
        help_text=_(
            "Each posted installment also moves its total to the reserve fund with an "
            "interfund entry, like the monthly reserve contribution."
        ),
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
    # Owner ratification (RCW 64.90.525(3) in WA): the board sends the owners a summary,
    # holds a ratification meeting in the rule pack's window, and records the outcome.
    # Nothing posts until it is ratified, when the rule pack requires ratification.
    summary_sent_on = models.DateField(_("summary sent to owners on"), null=True, blank=True)
    ratification_meeting_on = models.DateField(_("ratification meeting on"), null=True, blank=True)
    ratification = models.CharField(
        _("owner ratification"),
        max_length=10,
        choices=[
            ("ratified", _("Ratified by owners")),
            ("rejected", _("Rejected by owners")),
            ("emergency", _("Emergency: effective on the board's vote")),
        ],
        blank=True,
    )
    # Emergency (WA: RCW 64.90.502(6)): the board's vote makes it effective without owner
    # ratification, when the rule pack allows it; the owners are told right away.
    emergency_kind = models.CharField(
        _("kind of emergency"),
        max_length=20,
        blank=True,
        choices=[
            ("health_safety", _("Imminent threat to health or safety")),
            ("habitability", _("Imminent threat to the habitability of units")),
            ("economic_loss", _("Imminent risk of substantial economic loss to the association")),
        ],
    )
    emergency_reason = models.TextField(_("what happened"), blank=True)
    emergency_decided_on = models.DateField(_("board vote on"), null=True, blank=True)
    emergency_votes_for = models.PositiveSmallIntegerField(
        _("board members voting for"), null=True, blank=True
    )
    emergency_board_size = models.PositiveSmallIntegerField(_("board seats"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("special assessment")
        verbose_name_plural = _("special assessments")
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total__gt=0), name="billing_special_total_positive"
            )
        ]

    def __str__(self) -> str:
        return self.name


class SpecialInstallment(AssociationScopedModel):
    special = models.ForeignKey(
        SpecialAssessment,
        on_delete=models.PROTECT,
        related_name="installments",
        verbose_name=_("special assessment"),
    )
    number = models.PositiveSmallIntegerField(_("installment"))
    amount = MoneyField(_("amount"))
    charge_date = models.DateField(_("charge date"))
    due_date = models.DateField(_("due date"))
    posted_at = models.DateTimeField(_("posted at"), null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("posted by"),
    )
    source = models.CharField(_("source"), max_length=20, blank=True)
    reserve_entry = models.OneToOneField(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("reserve transfer entry"),
    )
    charges = models.ManyToManyField(
        "billing.Charge", blank=True, related_name="+", verbose_name=_("charges")
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("special assessment installment")
        verbose_name_plural = _("special assessment installments")
        ordering = ["special", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["special", "number"], name="billing_special_installment_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="billing_installment_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.special} {self.number}"

    @property
    def posted(self) -> bool:
        return self.posted_at is not None
