from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.dashboard"
    label = "dashboard"
    verbose_name = _("Dashboard")

    def ready(self):
        from homeoa.plugins import slots

        from . import todos

        todos.register_core_providers()
        slots.register(slots.HOME_CARDS, "dashboard/_home_whats_next.html", order=-10)
        slots.register(slots.HOME_CARDS, "dashboard/_home_money_cards.html", order=0)
        slots.register(slots.HOME_CARDS, "dashboard/_home_activity.html", order=20)
