"""Module manifests used by the module and slot tests."""

from homeoa.plugins import slots
from homeoa.plugins.manifest import CORE_VERSION, PluginManifest


def register_slots():
    slots.register(slots.HOME_CARDS, "sample card", module="sample")


sample = PluginManifest(
    id="sample",
    name="Sample",
    version=CORE_VERSION,
    kind="module",
    ready="homeoa.plugins.tests.fixtures.modules.register_slots",
)
needs_missing = PluginManifest(
    id="needs_missing", name="Needs", version=CORE_VERSION, kind="module", requires=("nope",)
)
