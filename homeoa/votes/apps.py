from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class VotesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.votes"
    label = "votes"
    verbose_name = _("Owner votes")

    def ready(self):
        from homeoa import audit

        from . import models

        for model in (models.Vote, models.Question, models.Choice, models.Ballot):
            audit.register(model)

        from homeoa.ui.nav import SubTab, register_subtab

        from . import issue

        issue.register()
        register_subtab(
            "governance",
            SubTab("votes", _("Owner votes"), "votes:index", _can_administer),
        )


def _can_administer(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.VOTES_ADMINISTER, association=getattr(request, "association", None))
