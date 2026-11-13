"""Annual budgets (FIN-13). Write through :mod:`homeoa.budget.services`.

Line amounts are annual and positive in the account's normal direction (income lines are
income, expense lines are spending). Budgets never post to the ledger; an adopted budget
only feeds the assessment schedule (FIN-03) and budget vs actual.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel


class BudgetStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ADOPTED = "adopted", _("Adopted")
    SUPERSEDED = "superseded", _("Superseded")


class Budget(AssociationScopedModel):
    year = models.PositiveSmallIntegerField(_("fiscal year"))
    name = models.CharField(_("name"), max_length=100)
    status = models.CharField(
        _("status"), max_length=12, choices=BudgetStatus.choices, default=BudgetStatus.DRAFT
    )
    notes = models.TextField(_("notes"), blank=True)
    adopted_at = models.DateTimeField(_("adopted at"), null=True, blank=True)
    adopted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("adopted by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    # Owner ratification (GOV-03): "ratified", or "rejected" (the prior budget continues).
    ratification = models.CharField(
        _("owner ratification"),
        max_length=10,
        choices=[("ratified", _("Ratified by owners")), ("rejected", _("Rejected by owners"))],
        blank=True,
    )
    ratification_on = models.DateField(_("ratification decided on"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("budget")
        verbose_name_plural = _("budgets")
        ordering = ["-year", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "year"],
                condition=models.Q(status="adopted"),
                name="budget_one_adopted_per_year",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_draft(self) -> bool:
        return self.status == BudgetStatus.DRAFT

    @property
    def is_adopted(self) -> bool:
        return self.status == BudgetStatus.ADOPTED


class BudgetLine(AssociationScopedModel):
    budget = models.ForeignKey(
        Budget, on_delete=models.CASCADE, related_name="lines", verbose_name=_("budget")
    )
    group = models.CharField(_("group"), max_length=60)
    label = models.CharField(_("line"), max_length=120)
    account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT, related_name="+", verbose_name=_("account")
    )
    fund = models.ForeignKey(
        "ledger.Fund", on_delete=models.PROTECT, related_name="+", verbose_name=_("fund")
    )
    amount = MoneyField(_("annual amount"), default=ZERO)
    note = models.CharField(_("note"), max_length=255, blank=True)
    sort_order = models.PositiveSmallIntegerField(_("order"), default=0)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("budget line")
        verbose_name_plural = _("budget lines")
        ordering = ["sort_order", "pk"]

    def __str__(self) -> str:
        return self.label
