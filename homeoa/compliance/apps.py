from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ComplianceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.compliance"
    label = "compliance"
    verbose_name = _("Compliance")

    def ready(self):
        from homeoa import audit
        from homeoa.community.services import register_source
        from homeoa.ui.nav import SubTab, register_subtab

        from . import calendar
        from .models import Evidence, ManualItem, ObligationPeriod

        audit.register(ObligationPeriod)
        audit.register(Evidence)
        audit.register(ManualItem)
        calendar.register_notification_type()
        register_source(calendar.SOURCE_KEY, _("Compliance deadlines"), calendar.calendar_source)
        register_subtab(
            "governance",
            SubTab("compliance", _("Compliance calendar"), "compliance:index", _can_read),
        )


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.BOARD_SHELL, association=getattr(request, "association", None))
