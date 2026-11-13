from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SetupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.setup"
    label = "setup"
    verbose_name = _("Association setup")

    def ready(self):
        from . import services

        services.register()
