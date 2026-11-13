from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RegistryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.registry"
    label = "registry"
    verbose_name = _("Registry")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from . import events  # noqa: F401 - registers event types
        from .models import (
            NoticePreference,
            Occupancy,
            OwnershipPeriod,
            OwnershipShare,
            Person,
            RecordsRequest,
            Unit,
        )

        for model in (
            Unit,
            Person,
            OwnershipPeriod,
            OwnershipShare,
            Occupancy,
            NoticePreference,
            RecordsRequest,
        ):
            audit.register(model)
        register_subtab(
            "documents",
            SubTab(
                "records-requests",
                _("Records requests"),
                "registry:records_requests",
                _can_manage_units,
            ),
        )
        from . import records

        records.register_notification_types()


def _can_manage_units(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.UNITS_MANAGE, association=getattr(request, "association", None))


def _can_read_directory(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.DIRECTORY_READ, association=getattr(request, "association", None))
