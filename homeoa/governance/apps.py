from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class GovernanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.governance"
    label = "governance"
    verbose_name = _("Governance")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from . import models

        for model in (
            models.BoardTerm,
            models.Committee,
            models.CommitteeMember,
            models.Resolution,
            models.ResolutionVote,
            models.ActionItem,
            models.ContactMessage,
        ):
            audit.register(model)
        register_subtab(
            "governance",
            SubTab("board", _("Board & committees"), "governance:index", _can_read),
        )
        register_subtab("community", SubTab("messages", _("Messages"), "contacts:inbox", _can_read))
        from . import contacts

        contacts.register_notification_type()


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.GOVERNANCE_READ, association=getattr(request, "association", None))
