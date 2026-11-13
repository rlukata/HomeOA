from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class EnforcementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.enforcement"
    label = "enforcement"
    verbose_name = _("Enforcement")

    def ready(self):
        from homeoa import audit

        from . import models

        for model in (
            models.ViolationCategory,
            models.FineSchedule,
            models.FineScheduleLine,
            models.Violation,
            models.ViolationPhoto,
            models.ViolationStep,
            models.Hearing,
            models.ViolationResponse,
        ):
            audit.register(model)
        from . import ladder

        ladder.register_notification_type()
        from . import responses

        responses.register_notification_type()
        from . import contributors

        contributors.register()
        ladder.register_merge_fields()


def _can_manage(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    association = getattr(request, "association", None)
    return has_perm(user, perms.VIOLATIONS_MANAGE, association=association)
