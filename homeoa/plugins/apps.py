from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _


class PluginsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.plugins"
    label = "homeoa_plugins"
    verbose_name = _("Plugins")

    def ready(self):
        from homeoa import audit

        from . import registry, services
        from .models import ModuleSwitch, PluginInstallation

        audit.register(PluginInstallation, exclude=("updated_at",))
        audit.register(ModuleSwitch)
        try:
            registry.load(getattr(settings, "HOMEOA_PLUGINS", []))
            registry.load_modules(getattr(settings, "HOMEOA_MODULES", []))
        except registry.PluginError as exc:
            raise ImproperlyConfigured(f"HomeOA plugin error: {exc}") from exc
        services.wire_event_handlers()
        # Module ready hooks run from homeoa.plugins.hooks, after every app is ready.
