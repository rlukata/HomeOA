"""Module manifest for committees; listed in ``settings.HOMEOA_MODULES``."""

from django.utils.translation import gettext_lazy as _

from homeoa.plugins.manifest import CORE_VERSION, PluginManifest

MANIFEST = PluginManifest(
    id="committees",
    name=_("Committees"),
    version=CORE_VERSION,
    description=_("Committees and their members."),
    kind="module",
    code=("homeoa/governance/committees_module.py",),
    pages=("governance:committee_add", "governance:member_add"),
)
