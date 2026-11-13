from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ResaleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.resale"
    label = "resale"
    verbose_name = _("Resale")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from . import services
        from .models import ResaleRequest

        audit.register(ResaleRequest)
        services.register_notification_type()
        register_subtab(
            "units",
            SubTab("resale", _("Resale certificates"), "resale:index", _can_manage_units),
        )


def _can_manage_units(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.UNITS_MANAGE, association=getattr(request, "association", None))
