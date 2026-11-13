from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ChangesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.changes"
    label = "changes"
    verbose_name = _("Change requests")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from . import notifications
        from .models import ChangeDecision, ChangeRequest

        audit.register(ChangeRequest)
        audit.register(ChangeDecision)
        notifications.register_notification_types()
        register_subtab(
            "governance", SubTab("changes", _("Change requests"), "changes:index", _can_list)
        )


def _can_list(request) -> bool:
    from homeoa.access.services import is_board_member

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return is_board_member(user, getattr(request, "association", None))
