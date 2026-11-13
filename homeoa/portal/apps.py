from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.portal"
    label = "portal"
    verbose_name = _("Resident portal")
