from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class SearchConfig(AppConfig):
    name = "homeoa.search"
    label = "search"
    verbose_name = _("Search")

    def ready(self):
        from . import sources

        sources.register()
