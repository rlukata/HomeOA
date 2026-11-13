"""General ledger models (FIN-01, FIN-02). Accrual basis.

Write path: only ``homeoa.ledger.services.post_entry`` / ``reverse_entry`` create entries.
Posted entries and lines are immutable: the model ``save``/``delete`` guards refuse changes,
and Postgres triggers (migration 0002) reject UPDATE/DELETE and check, at commit time, that
every entry balances per fund.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel


class LedgerImmutableError(Exception):
    """Raised when code tries to change or delete a posted entry or line."""


class Fund(AssociationScopedModel):
    """An accounting fund (operating, reserve, ...). Every journal line belongs to one."""

    code = models.SlugField(_("code"), max_length=32)
    name = models.CharField(_("name"), max_length=100)
    is_reserve = models.BooleanField(_("reserve fund"), default=False)
    active = models.BooleanField(_("active"), default=True)
    sort_order = models.PositiveSmallIntegerField(_("sort order"), default=0)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("fund")
        verbose_name_plural = _("funds")
        ordering = ["sort_order", "code"]
        constraints = [
            models.UniqueConstraint(fields=["association", "code"], name="ledger_fund_unique_code")
        ]

    def __str__(self) -> str:
        return self.name


class AccountType(models.TextChoices):
    ASSET = "asset", _("Asset")
    LIABILITY = "liability", _("Liability")
    EQUITY = "equity", _("Equity / fund balance")
    INCOME = "income", _("Income")
    EXPENSE = "expense", _("Expense")


DEBIT_NORMAL = frozenset({AccountType.ASSET, AccountType.EXPENSE})


class Account(AssociationScopedModel):
    """A chart-of-accounts entry. Accounts are shared by all funds; the fund is per line."""

    code = models.CharField(_("code"), max_length=20)
    name = models.CharField(_("name"), max_length=120)
    type = models.CharField(_("type"), max_length=12, choices=AccountType.choices)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("parent"),
    )
    active = models.BooleanField(_("active"), default=True)
    in_unit_subledger = models.BooleanField(
        _("in unit subledger"),
        default=False,
        help_text=_("Lines on this account make up each unit's balance (receivable and prepaid)."),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("account")
        verbose_name_plural = _("accounts")
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "code"], name="ledger_account_unique_code"
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @property
    def debit_normal(self) -> bool:
        return self.type in DEBIT_NORMAL


class LedgerSettings(AssociationScopedModel):
    """Per-association ledger settings. ``locked_through`` is the period-lock hook.

    Reconciliation and period close move ``locked_through`` forward; no entry
    may be dated on or before it.
    """

    locked_through = models.DateField(_("locked through"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("ledger settings")
        verbose_name_plural = _("ledger settings")
        constraints = [
            models.UniqueConstraint(fields=["association"], name="ledger_settings_one_per_assoc")
        ]


class _Immutable(AssociationScopedModel):
    class Meta(AssociationScopedModel.Meta):
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise LedgerImmutableError(_("Posted ledger records cannot be changed."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutableError(_("Posted ledger records cannot be deleted."))


class JournalEntry(_Immutable):
    """One balanced posting. Correct mistakes with ``reverse_entry``, never by editing."""

    date = models.DateField(_("date"), db_index=True)
    memo = models.CharField(_("memo"), max_length=255, blank=True)
    source_type = models.CharField(
        _("source type"), max_length=64, blank=True, help_text=_("e.g. billing.charge")
    )
    source_id = models.CharField(_("source id"), max_length=64, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("posted by"),
    )
    posted_at = models.DateTimeField(_("posted at"), auto_now_add=True)
    sealed_at = models.DateTimeField(
        _("sealed at"),
        null=True,
        blank=True,
        help_text=_("Set by the posting service once all lines are in; no lines after that."),
    )
    reversal_of = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        verbose_name=_("reversal of"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("journal entry")
        verbose_name_plural = _("journal entries")
        ordering = ["date", "pk"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self) -> str:
        return f"JE {self.pk} {self.date:%Y-%m-%d} {self.memo}".strip()


class JournalLine(_Immutable):
    """A debit or credit to one account in one fund, optionally tagged to a unit.

    Subledger tags ``unit`` / ``ownership_period`` are PROTECT foreign keys into the registry
    (attnames ``unit_id`` / ``ownership_period_id``, the original column names), so a unit or
    ownership period with ledger history can never be deleted.
    """

    entry = models.ForeignKey(
        JournalEntry, on_delete=models.PROTECT, related_name="lines", verbose_name=_("entry")
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="lines", verbose_name=_("account")
    )
    fund = models.ForeignKey(
        Fund, on_delete=models.PROTECT, related_name="lines", verbose_name=_("fund")
    )
    debit = MoneyField(_("debit"), default=ZERO)
    credit = MoneyField(_("credit"), default=ZERO)
    memo = models.CharField(_("memo"), max_length=255, blank=True)
    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("unit"),
    )
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("ownership period"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("journal line")
        verbose_name_plural = _("journal lines")
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(debit__gt=0, credit=0) | models.Q(debit=0, credit__gt=0)),
                name="ledger_line_one_sided_positive",
            )
        ]

    @property
    def amount(self):
        """Signed amount, debit positive."""
        return self.debit - self.credit
