from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ClosingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.closing"
    label = "closing"
    verbose_name = _("Period close")

    def ready(self):
        import dataclasses

        from homeoa.ui.nav import SubTab, get_hub, register_hub

        from . import events  # noqa: F401  (registers event types)

        # Money > Close, after Reports.
        hub = get_hub("money")
        if not any(t.slug == "close" for t in hub.subtabs):
            tab = SubTab("close", _("Close"), "closing:index", _can_read)
            register_hub(dataclasses.replace(hub, subtabs=(*hub.subtabs, tab)))


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.FINANCE_READ, association=getattr(request, "association", None))
