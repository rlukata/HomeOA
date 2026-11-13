from django.apps import AppConfig, apps
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _


class ModuleHooksConfig(AppConfig):
    """Listed last in ``INSTALLED_APPS``, so module hooks see every app's registrations."""

    name = "homeoa.plugins.hooks"
    label = "homeoa_module_hooks"
    verbose_name = _("Module hooks")

    def ready(self):
        from homeoa.plugins import registry

        if list(apps.get_app_configs())[-1] is not self:
            raise ImproperlyConfigured(f"{self.name} must be the last entry in INSTALLED_APPS.")
        registry.run_module_ready_hooks()
