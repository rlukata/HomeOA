from typing import Literal

from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, Field

from homeoa.plugins.manifest import PluginManifest


class StripeSettings(BaseModel):
    webhook_secret: str = Field(
        "",
        title=_("Webhook signing secret"),
        description=_("Stripe signs webhooks with this (whsec_...). Stored encrypted."),
    )
    api_key: str = Field(
        "",
        title=_("Secret API key"),
        description=_("Stripe secret key (sk_...). Stored encrypted. Unused with the fake."),
    )
    fee_mode: Literal["absorb", "pass_through"] = Field(
        "absorb",
        title=_("Processing fees"),
        description=_(
            "absorb: the association pays the fee (posted as an expense). "
            "pass_through: the board opts in to adding the fee to what the owner pays; it "
            "applies only while the rule payments.fee_pass_through_allowed is on "
            "(unset in the WA pack, so off). Not shown on the board settings page."
        ),
        json_schema_extra={"hidden": True},
    )
    deposit_account: str = Field(
        "1060",
        title=_("Deposit account code"),
        description=_("Clearing account the payments land in until Stripe pays out to the bank."),
    )
    fee_account: str = Field(
        "5450",
        title=_("Fee expense account code"),
        description=_("Expense account for absorbed processing fees."),
    )


manifest = PluginManifest(
    id="stripe",
    name=_("Stripe ACH payments (test fake)"),
    version="0.1.0",
    core_requires=">=0.1,<0.2",
    settings_model=StripeSettings,
    secret_fields=("webhook_secret", "api_key"),
    providers={"PaymentProvider": "homeoa_stripe.provider.StripeACH"},
    webhook="homeoa_stripe.webhook.StripeWebhook",
    django_app="homeoa_stripe",
    urls="homeoa_stripe.urls",
    todos="homeoa_stripe.services.todo_items",
    description=_(
        "Owners pay by bank account (ACH) through Stripe. Only the built-in fake is "
        "available: no real money moves. HomeOA never sends payouts."
    ),
)
