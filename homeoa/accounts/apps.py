from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.accounts"
    label = "accounts"
    verbose_name = _("Accounts")

    def ready(self):
        from . import events

        events.connect()

        from homeoa import audit

        from .models import TOTPDevice

        # The secret is encrypted, so its diffs only ever say "changed".
        audit.register(TOTPDevice, exclude=("last_used_step",))
