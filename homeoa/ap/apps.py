from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ApConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.ap"
    label = "ap"
    verbose_name = _("Accounts payable")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from .models import Bill, BillAttachment, BillLine

        audit.register(Bill)
        audit.register(BillLine)
        audit.register(BillAttachment)

        from .approvals.models import ApprovalRule

        audit.register(ApprovalRule)

        from .approvals import notifications

        notifications.register_notification_types()
        register_subtab("money", SubTab("bills", _("Bills"), "ap:index"))
