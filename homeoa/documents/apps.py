from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DocumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "homeoa.documents"
    label = "documents"
    verbose_name = _("Documents")

    def ready(self):
        from homeoa import audit
        from homeoa.registry import unit_sections

        from . import contributors
        from .models import WelcomePacket

        audit.register(WelcomePacket)
        contributors.register_defaults()
        unit_sections.register("documents/_unit_file.html")

        from homeoa.ui import nav

        hub = nav.get_hub("documents")
        for tab in hub.subtabs:
            # Insurance is registered by homeoa.insurance (the policy above the documents).
            if tab.slug in ("all", "governing", "financial", "meetings", "welcome"):
                name = "index" if tab.slug == "all" else tab.slug
                nav.register_subtab(
                    "documents", nav.SubTab(tab.slug, tab.label, url_name=f"documents:{name}")
                )
