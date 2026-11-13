from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.jobs"
    label = "jobs"
    verbose_name = _("Jobs")

    def ready(self):
        from . import events  # noqa: F401  (registers jobs.* event types)
