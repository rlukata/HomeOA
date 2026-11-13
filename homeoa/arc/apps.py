from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ArcConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.arc"
    label = "arc"
    verbose_name = _("Architectural review")

    def ready(self):
        from homeoa import audit

        from . import deadlines, decision_letters, deemed, models, services

        for model in (
            models.ArcForm,
            models.ArcRequest,
            models.ArcAttachment,
            models.ArcComment,
            models.ArcVote,
        ):
            audit.register(model)
        services.register_notification_type()
        from . import contributors

        contributors.register()
        deadlines.register_notification_type()
        deemed.register_notification_type()
        decision_letters.register()


def can_review(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_any

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_any(
        user,
        (perms.ARCH_REVIEW, perms.ARCH_DECIDE),
        association=getattr(request, "association", None),
    )
