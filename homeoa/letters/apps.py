from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class LettersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.letters"
    label = "letters"
    verbose_name = _("Letters")

    def ready(self):
        from homeoa import audit

        from . import merge  # noqa: F401  (registers the built-in merge fields)
        from .batches import register_notification_type
        from .models import LetterTemplate, MailBatch

        audit.register(LetterTemplate)
        audit.register(MailBatch)
        register_notification_type()
