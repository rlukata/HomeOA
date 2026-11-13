from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.core"
    label = "core"
    verbose_name = _("Core")

    def ready(self):
        from . import (
            checks,  # noqa: F401  (registers system checks)
            retention,
        )

        retention.connect()  # Sent or posted records cannot be deleted
