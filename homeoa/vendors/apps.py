from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class VendorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.vendors"
    label = "vendors"
    verbose_name = _("Vendors")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from . import alerts
        from .models import Contract, Vendor, VendorContact, VendorDocument

        audit.register(Vendor)
        audit.register(VendorContact)
        audit.register(VendorDocument)
        audit.register(Contract)
        alerts.register_notification_type()
        register_subtab("vendors", SubTab("all", _("All vendors"), "vendors:index"))

        from . import vendor_changes

        vendor_changes.register()
