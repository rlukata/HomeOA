from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class MeetingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.meetings"
    label = "meetings"
    verbose_name = _("Meetings")

    def ready(self):
        from homeoa import audit
        from homeoa.community import services as community
        from homeoa.ui.nav import SubTab, register_subtab

        from . import calendar, models, services

        for model in (
            models.Meeting,
            models.AgendaItem,
            models.MeetingNotice,
            models.Minutes,
            models.Attendance,
            models.Proxy,
            models.AnnualWorkflow,
        ):
            audit.register(model)
        services.register_notification_type()
        services.register_merge_fields()
        register_subtab(
            "governance", SubTab("meetings", _("Meetings"), "meetings:index", _can_read)
        )
        community.register_source(calendar.SOURCE_KEY, _("Meetings"), calendar.events)


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.GOVERNANCE_READ, association=getattr(request, "association", None))
