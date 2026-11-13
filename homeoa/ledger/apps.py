from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.ledger"
    label = "ledger"
    verbose_name = _("Ledger")

    def ready(self):
        from homeoa.audit.services import register

        from . import events  # noqa: F401  (registers event types)
        from .models import Account, Fund, LedgerSettings

        # Entries are audited explicitly by the posting service (ledger.post / ledger.reverse).
        register(Account)
        register(Fund)
        register(LedgerSettings)
