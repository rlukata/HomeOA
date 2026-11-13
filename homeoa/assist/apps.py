from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AssistConfig(AppConfig):
    name = "homeoa.assist"
    label = "assist"
    verbose_name = _("AI assistance")
