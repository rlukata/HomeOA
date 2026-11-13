from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.billing"
    label = "billing"
    verbose_name = _("Billing")

    def ready(self):
        from homeoa import audit
        from homeoa.registry.services import register_transfer_validator

        from . import tasks  # noqa: F401 - registers the periodic job
        from .services import special

        special.register_notification_type()
        from .models import (
            Adjustment,
            AssessmentSchedule,
            BillingRun,
            Charge,
            LateFee,
            LateFeePolicy,
            SpecialAssessment,
            SpecialInstallment,
        )
        from .services import block_transfer_over_posted_charges

        for model in (
            AssessmentSchedule,
            BillingRun,
            Charge,
            Adjustment,
            LateFeePolicy,
            LateFee,
            SpecialAssessment,
            SpecialInstallment,
        ):
            audit.register(model)
        register_transfer_validator(block_transfer_over_posted_charges)
