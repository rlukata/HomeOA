from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class RulesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.rules"
    label = "rules"
    verbose_name = _("Rule packs")

    def ready(self):
        from . import lawcheck

        lawcheck.register_notification_type()
