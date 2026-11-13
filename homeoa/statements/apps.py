from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class StatementsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.statements"
    label = "statements"
    verbose_name = _("Statements")

    def ready(self):
        from homeoa import audit

        from . import owner_statements
        from .statement_models import Statement, StatementRun

        audit.register(StatementRun)
        audit.register(Statement)
        owner_statements.register_notification_type()
        from homeoa.registry import unit_sections

        unit_sections.register("statements/_unit_statements.html")

        from . import delinquency
        from .models import DelinquencyCase

        audit.register(DelinquencyCase)
        delinquency.register_notification_types()
        from homeoa.ui.nav import SubTab, register_subtab

        register_subtab(
            "money",
            SubTab("delinquency", _("Delinquency"), "statements:delinquency", _can_read),
        )
        # Demand-letter to-dos reach Home via dashboard CORE_PROVIDERS.


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.FINANCE_READ, association=getattr(request, "association", None))
