"""Module manifest for AI assistance; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="ai_assistance",
    name=_("AI assistance"),
    version=CORE_VERSION,
    description=_(
        "AI fills in forms from documents for a person to check, starting with a new bill "
        "from an invoice. Needs an AI provider, which the System admin connects; each use "
        "sends that document to the provider."
    ),
    kind="module",
    code=("homeoa/assist",),
    pages=("assist:*",),
)
