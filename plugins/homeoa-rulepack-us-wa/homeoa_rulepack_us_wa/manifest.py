from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import PluginManifest

manifest = PluginManifest(
    id="rulepack_us_wa",
    name=_("Washington condo rules"),
    version="0.2.0",
    core_requires=">=0.1,<0.2",
    providers={"RulePack": "homeoa_rulepack_us_wa.pack.pack"},
    description=_(
        "Washington condominium rule values checked against the statute text on Oct 8, "
        "2026. Some need confirmation with an attorney. Not legal advice."
    ),
)
