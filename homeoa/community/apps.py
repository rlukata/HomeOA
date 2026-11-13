from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CommunityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.community"
    label = "community"
    verbose_name = _("Community")

    def ready(self):
        from homeoa import audit

        from . import services
        from .models import Announcement, CalendarEvent

        audit.register(Announcement)
        audit.register(CalendarEvent)
        from .models import Poll

        audit.register(Poll)
        services.register_notification_type()


def _can_manage(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(
        user, perms.ANNOUNCEMENTS_MANAGE, association=getattr(request, "association", None)
    )
