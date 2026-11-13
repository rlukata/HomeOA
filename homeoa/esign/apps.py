from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class ESignConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.esign"
    label = "esign"
    verbose_name = _("E-signatures")

    def ready(self):
        from homeoa import audit
        from homeoa.ui.nav import SubTab, register_subtab

        from . import services
        from .models import Envelope, Signer

        audit.register(Envelope)
        audit.register(Signer, exclude=("token_hash",))
        services.register_notification_type()
        register_subtab(
            "governance", SubTab("signatures", _("Signatures"), "esign:index", _can_read)
        )


def _can_read(request) -> bool:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    return has_perm(user, perms.BOARD_SHELL, association=getattr(request, "association", None))
