from django.apps import AppConfig
from django.db.models.signals import post_save
from django.utils.translation import gettext_lazy as _


def _seed_roles(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from .services import ensure_default_roles

        ensure_default_roles(instance)


def _hub_check(hub_id):
    from .permissions import HUB_PERMISSIONS
    from .services import has_any

    codes = HUB_PERMISSIONS.get(hub_id, ())

    def check(request) -> bool:
        return has_any(
            getattr(request, "user", None), codes, association=getattr(request, "association", None)
        )

    return check


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.access"
    label = "access"
    verbose_name = _("Access control")

    def ready(self):
        from homeoa import audit
        from homeoa.core.models import Association
        from homeoa.ui import nav

        from .models import Role, RoleAssignment

        audit.register(Role)
        audit.register(RoleAssignment)
        from homeoa.registry import unit_sections

        from . import invitations

        invitations.register_type()
        unit_sections.register("access/_unit_access.html")
        post_save.connect(_seed_roles, sender=Association, dispatch_uid="access_seed_roles")
        nav.set_permission_resolver(_hub_check)

        from . import role_changes

        role_changes.register()
