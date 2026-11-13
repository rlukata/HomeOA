"""Stripe records: ACH mandates, payment intents (one row per intent) and payouts."""

from django.conf import settings
from django.db import models
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import MoneyField
from homeoa.core.models import AssociationScopedModel


class IntentStatus(models.TextChoices):
    PROCESSING = "processing", _("Processing")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")


class FeeMode(models.TextChoices):
    ABSORB = "absorb", _("Absorbed by the association")
    PASS_THROUGH = "pass_through", _("Paid by the owner")


class Verification(models.TextChoices):
    INSTANT = "instant", _("Instant (Financial Connections)")
    MICRODEPOSITS = "microdeposits", _("Microdeposits")


class MandateRetained(Exception):
    """ACH authorizations are never deleted (Nacha keeps them 2 years after revocation)."""


class StripeMandate(AssociationScopedModel):
    """An owner's ACH debit authorization for one unit (NACHA-style record).

        Never deleted: revoking sets ``revoked_at`` and keeps the text and proof of acceptance
    . ``delete()`` and queryset deletes raise ``MandateRetained``; a future retention
        purge must use ``mandates.purgeable`` (revoked at least 2 years ago).

        ``authorization_version`` names the exact text the owner accepted (see
        ``mandates.AUTHORIZATION_TEXTS``); the text itself is copied into ``authorization_text``
        so later wording changes never rewrite what was agreed.

        A mandate can be charged only once it is verified (``verified_at``: Financial
        Connections at once, microdeposits later) and while it has Stripe's ``customer``,
        ``payment_method`` and mandate ids (``chargeable``).
    """

    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    owner = models.ForeignKey(
        "registry.Person", on_delete=models.PROTECT, related_name="+", verbose_name=_("owner")
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("accepted by"),
    )
    stripe_mandate_id = models.CharField(_("Stripe mandate id"), max_length=64, blank=True)
    stripe_customer_id = models.CharField(_("Stripe customer id"), max_length=64, blank=True)
    stripe_payment_method_id = models.CharField(
        _("Stripe payment method id"), max_length=64, blank=True
    )
    setup_intent_id = models.CharField(_("Stripe setup intent id"), max_length=64, blank=True)
    bank_name = models.CharField(_("bank"), max_length=100, blank=True)
    verification = models.CharField(
        _("verification"), max_length=16, choices=Verification.choices, blank=True
    )
    verified_at = models.DateTimeField(_("verified at"), null=True, blank=True)
    bank_last4 = models.CharField(_("bank account last 4"), max_length=4)
    authorization_version = models.CharField(_("authorization version"), max_length=32)
    authorization_text = models.TextField(_("authorization text"))
    accepted_at = models.DateTimeField(_("accepted at"))
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    user_agent = models.CharField(_("user agent"), max_length=500, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("ACH mandate")
        verbose_name_plural = _("ACH mandates")
        ordering = ["-accepted_at", "-pk"]

    def __str__(self):
        return f"{self.unit} ····{self.bank_last4}"

    def delete(self, *args, **kwargs):
        raise MandateRetained(self.pk)

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @property
    def chargeable(self) -> bool:
        return bool(
            self.active
            and self.verified_at is not None
            and self.stripe_mandate_id
            and self.stripe_customer_id
            and self.stripe_payment_method_id
        )


class StripeIntent(AssociationScopedModel):
    """An ACH payment intent. ``amount`` pays charges; ``charged`` is what the payer's
    bank is debited (``amount`` + ``fee`` when the fee is passed through)."""

    intent_id = models.CharField(_("intent id"), max_length=64)
    unit = models.ForeignKey(
        "registry.Unit", on_delete=models.PROTECT, related_name="+", verbose_name=_("unit")
    )
    #: The payment applies to the ownership period it was created for.
    ownership_period = models.ForeignKey(
        "registry.OwnershipPeriod",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("ownership period"),
    )
    amount = MoneyField(_("amount"))
    fee = MoneyField(_("fee"))
    charged = MoneyField(_("charged"))
    fee_mode = models.CharField(_("fees"), max_length=12, choices=FeeMode.choices)
    status = models.CharField(
        _("status"),
        max_length=12,
        choices=IntentStatus.choices,
        default=IntentStatus.PROCESSING,
    )
    mandate = models.CharField(_("mandate"), max_length=64, blank=True)
    mandate_record = models.ForeignKey(
        StripeMandate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="intents",
        verbose_name=_("mandate record"),
    )
    #: The fee Stripe actually charged (from the balance transaction); ``fee`` is the estimate.
    actual_fee = MoneyField(_("actual fee"), null=True, blank=True)
    #: Set when a succeeded payment later failed (ACH return) and its receipt was voided.
    returned_at = models.DateTimeField(_("returned at"), null=True, blank=True)
    return_acknowledged_at = models.DateTimeField(
        _("return acknowledged at"), null=True, blank=True
    )
    message = models.CharField(_("message"), max_length=500, blank=True)
    #: Pay now / autopay key: a repeat with the same key returns the first intent.
    idempotency_key = models.CharField(_("idempotency key"), max_length=100, blank=True)
    receipt = models.ForeignKey(
        "payments.Receipt",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("receipt"),
    )
    fee_entry = models.ForeignKey(
        "ledger.JournalEntry",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("fee entry"),
    )
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("Stripe payment intent")
        verbose_name_plural = _("Stripe payment intents")
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["intent_id"], name="stripe_intent_unique")]

    def __str__(self):
        return self.intent_id


class PayoutStatus(models.TextChoices):
    PAID = "paid", _("Paid")
    FAILED = "failed", _("Failed")


class StripePayout(AssociationScopedModel):
    """A payout Stripe sent from the clearing balance (1060) to the operating bank account.

    HomeOA never creates payouts; it only learns of them from webhooks and proposes the
    match to the bank deposit. ``match`` is the banking match that moved the money from
    1060 to cash; an undone match counts as unmatched.
    """

    payout_id = models.CharField(_("payout id"), max_length=64)
    amount = MoneyField(_("amount"))
    arrival_date = models.DateField(_("arrival date"))
    status = models.CharField(
        _("status"), max_length=12, choices=PayoutStatus.choices, default=PayoutStatus.PAID
    )
    match = models.ForeignKey(
        "banking.Match",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("bank match"),
    )
    created_at = models.DateTimeField(_("created"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("Stripe payout")
        verbose_name_plural = _("Stripe payouts")
        ordering = ["-arrival_date", "-pk"]
        constraints = [models.UniqueConstraint(fields=["payout_id"], name="stripe_payout_unique")]

    def __str__(self):
        return self.payout_id

    @property
    def matched(self) -> bool:
        return self.match_id is not None and self.match.undone_at is None


@receiver(pre_delete, sender=StripeMandate)
def _refuse_mandate_delete(sender, instance, **kwargs):
    raise MandateRetained(instance.pk)
