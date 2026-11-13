from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class StripeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa_stripe"
    label = "homeoa_stripe"
    verbose_name = _("Stripe ACH payments")

    def ready(self):
        from homeoa import audit
        from homeoa.banking import matching

        from .client import check_settings_keys
        from .models import StripeIntent, StripeMandate, StripePayout
        from .services import payout_suggestions

        for model in (StripeIntent, StripeMandate, StripePayout):
            audit.register(model)
        matching.register_suggester("stripe.payouts", payout_suggestions)
        check_settings_keys()
