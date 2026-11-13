from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.payments"
    label = "payments"
    verbose_name = _("Payments")

    def ready(self):
        from django.db.models.signals import post_save

        from homeoa import audit
        from homeoa.billing.models import Charge

        from .models import (
            Allocation,
            AutopayCharge,
            AutopayEnrollment,
            AutopayRun,
            Receipt,
            Refund,
        )
        from .signals import charge_created

        for model in (Receipt, Allocation, Refund, AutopayEnrollment, AutopayCharge, AutopayRun):
            audit.register(model)
        post_save.connect(charge_created, sender=Charge, dispatch_uid="payments.charge_created")
