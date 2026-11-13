from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.audit"
    label = "audit"
    verbose_name = _("Audit")

    def ready(self):
        from homeoa.core.models import Association

        from .services import register

        # Changes to association settings show up as board-visible "setting" events.
        register(Association)

        from . import receivers

        receivers.connect()
