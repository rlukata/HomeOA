from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class NotifyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.notify"
    label = "notify"
    verbose_name = _("Notifications")

    def ready(self):
        from homeoa.plugins import registry
        from homeoa.registry import unit_sections

        from . import bounces, channels

        channels.register_defaults()
        registry.register_builtin(bounces.manifest)
        unit_sections.register("notify/_unit_comms.html")
        # Failed and bounced deliveries reach Home via dashboard CORE_PROVIDERS.
