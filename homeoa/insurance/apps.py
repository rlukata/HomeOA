from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class InsuranceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.insurance"
    label = "insurance"
    verbose_name = _("Insurance")

    def ready(self):
        from homeoa import audit
        from homeoa.ui import nav

        from .models import MasterPolicy

        audit.register(MasterPolicy)
        # The policy sits above the insurance documents on Documents > Insurance.
        hub = nav.get_hub("documents")
        label = next(t.label for t in hub.subtabs if t.slug == "insurance")
        nav.register_subtab("documents", nav.SubTab("insurance", label, "insurance:index"))
