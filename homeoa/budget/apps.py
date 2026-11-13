from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BudgetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.budget"
    label = "budget"
    verbose_name = _("Budget")

    def ready(self):
        import dataclasses

        from homeoa import audit
        from homeoa.ui.nav import SubTab, get_hub, register_hub

        from .models import Budget, BudgetLine

        audit.register(Budget)
        audit.register(BudgetLine)
        # Fill the Money > Budget placeholder in place (keeps the prototype tab order).
        hub = get_hub("money")
        tab = SubTab("budget", _("Budget"), "budget:index", _can_read)
        tabs = tuple(tab if t.slug == "budget" else t for t in hub.subtabs)
        register_hub(dataclasses.replace(hub, subtabs=tabs))


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.FINANCE_READ, association=getattr(request, "association", None))
