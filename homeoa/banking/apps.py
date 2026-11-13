from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class BankingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.banking"
    label = "banking"
    verbose_name = _("Banking")

    def ready(self):
        from homeoa.audit.services import register

        from . import events  # noqa: F401  (registers event types)
        from .models import BankAccount, CategorizeRule, CsvColumnMapping, PayerAlias

        # Transactions and batches are audited explicitly by the import service.
        register(BankAccount)
        register(CsvColumnMapping)
        # Matches and reconciliations are audited explicitly by matching / reconcile.
        register(CategorizeRule)
        register(PayerAlias)
