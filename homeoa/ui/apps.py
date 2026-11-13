from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class UiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.ui"
    label = "ui"
    verbose_name = _("User interface")
