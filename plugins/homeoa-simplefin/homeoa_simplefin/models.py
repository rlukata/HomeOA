"""Sync state and the SimpleFIN account -> BankAccount mapping."""

import secrets

from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.banking.models import BankAccount
from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class ErrorKind(models.TextChoices):
    NONE = "", _("None")
    AUTH = "auth", _("Access expired or revoked")
    PAYMENT = "payment", _("SimpleFIN subscription needs payment")
    NETWORK = "network", _("Couldn't reach SimpleFIN")
    QUOTA = "quota", _("Daily SimpleFIN request limit reached")
    OTHER = "other", _("Unexpected response")


def random_sync_minute() -> int:
    """A random minute of the local day for the daily sync (spreads load)."""
    return secrets.randbelow(24 * 60)


class Connection(AssociationScopedModel):
    """One per association: when we last synced and what went wrong."""

    last_synced_at = models.DateTimeField(_("last synced"), null=True, blank=True)
    last_attempt_at = models.DateTimeField(_("last attempt"), null=True, blank=True)
    error_kind = models.CharField(
        _("error"), max_length=10, choices=ErrorKind.choices, blank=True, default=""
    )
    error_message = models.CharField(_("error message"), max_length=500, blank=True)
    error_at = models.DateTimeField(_("error at"), null=True, blank=True)
    #: Warnings from the last sync ("errors" and "x-api-message" in /accounts).
    bank_messages = models.JSONField(_("bank messages"), default=list, blank=True)
    #: Errors from the last sync ("errlist" in /accounts); the board must act on these.
    bank_errors = models.JSONField(_("bank errors"), default=list, blank=True)
    #: Minute of the association's local day when the daily sync runs.
    sync_minute = models.PositiveSmallIntegerField(
        _("daily sync minute"), default=random_sync_minute
    )
    #: Requests made to the bridge today (UTC) with this access URL, per quota.
    request_day = models.DateField(_("request day"), null=True, blank=True)
    request_count = models.PositiveSmallIntegerField(_("all-accounts requests"), default=0)
    account_request_count = models.PositiveSmallIntegerField(
        _("single-account requests"), default=0
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("SimpleFIN connection")
        verbose_name_plural = _("SimpleFIN connections")
        constraints = [
            models.UniqueConstraint(fields=["association"], name="simplefin_connection_unique")
        ]

    def __str__(self):
        return f"SimpleFIN ({self.association_id})"


class SimpleFinAccount(AssociationScopedModel):
    """An account SimpleFIN reports, optionally mapped to one of our bank accounts."""

    sf_id = models.CharField(_("SimpleFIN account id"), max_length=255)
    name = models.CharField(_("name"), max_length=255, blank=True)
    org_name = models.CharField(_("bank"), max_length=255, blank=True)
    currency = models.CharField(_("currency"), max_length=10, blank=True)
    balance = MoneyField(_("balance"), null=True, blank=True)
    balance_date = models.DateField(_("balance date"), null=True, blank=True)
    bank_account = models.OneToOneField(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="simplefin_account",
        verbose_name=_("bank account"),
    )
    #: The board chose not to import this account (no to-do nags about it).
    ignored = models.BooleanField(_("don't import"), default=False)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("SimpleFIN account")
        verbose_name_plural = _("SimpleFIN accounts")
        ordering = ["org_name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["association", "sf_id"], name="simplefin_account_unique"
            )
        ]

    def __str__(self):
        return f"{self.org_name} {self.name}".strip()
