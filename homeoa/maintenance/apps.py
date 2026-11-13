from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MaintenanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.maintenance"
    label = "maintenance"
    verbose_name = _("Maintenance")

    def ready(self):
        from homeoa import audit

        from .models import (
            Component,
            MaintenancePhoto,
            MaintenanceRequest,
            PreventiveTask,
            WorkOrder,
            WorkOrderBill,
        )

        audit.register(MaintenanceRequest)
        audit.register(MaintenancePhoto)
        audit.register(WorkOrder)
        audit.register(WorkOrderBill)
        audit.register(Component)
        audit.register(PreventiveTask)
        from . import contributors

        contributors.register()
        from . import oneoff

        oneoff.register()


def _can_manage(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(
        user, perms.MAINTENANCE_MANAGE, association=getattr(request, "association", None)
    )
