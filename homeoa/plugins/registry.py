"""Plugin discovery and the in-memory registry of loaded plugins.

Discovery reads the ``homeoa.plugins`` entry-point group and keeps only the plugins listed
in ``settings.HOMEOA_PLUGINS`` (matched by entry-point name, which should equal the plugin
id, or by distribution name). A listed plugin that is missing, broken or incompatible with
this core version raises ``PluginError`` at startup. With an empty list no plugin code is
imported.

Importable before Django settings are configured (see ``django_apps``).
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from importlib.metadata import EntryPoint, entry_points

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from .manifest import CORE_VERSION, PluginManifest

GROUP = "homeoa.plugins"


class PluginError(Exception):
    """A listed plugin cannot be loaded. The message says which one and why."""


_loaded: dict[str, PluginManifest] = {}
#: Manifests that core apps ship themselves (e.g. ``email_bounces``). They are always
#: loaded, survive ``load``/``clear``, and are still enabled per association like any plugin.
_builtin: dict[str, PluginManifest] = {}

#: Optional modules shipped inside HomeOA (``kind="module"``), loaded from the dotted
#: manifest paths in ``settings.HOMEOA_MODULES``. They are not listed as plugins.
_modules: dict[str, PluginManifest] = {}


def _entry_points() -> Iterable[EntryPoint]:
    return entry_points(group=GROUP)


def _matches(ep: EntryPoint, wanted: set[str]) -> str | None:
    if ep.name in wanted:
        return ep.name
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None) if dist else None
    if name and name in wanted:
        return name
    return None


def check_compatible(manifest: PluginManifest, core_version: str = CORE_VERSION) -> None:
    if not manifest.core_requires:
        return
    try:
        spec = SpecifierSet(manifest.core_requires)
    except InvalidSpecifier as exc:
        raise PluginError(
            f"Plugin {manifest.id!r} has an invalid core_requires {manifest.core_requires!r}."
        ) from exc
    if not spec.contains(Version(core_version), prereleases=True):
        raise PluginError(
            f"Plugin {manifest.id!r} {manifest.version} requires HomeOA core "
            f"{manifest.core_requires}, but this is core {core_version}. "
            "Upgrade the plugin or remove it from HOMEOA_PLUGINS."
        )


def discover(
    enabled: Iterable[str], eps: Iterable[EntryPoint] | None = None
) -> dict[str, PluginManifest]:
    """Load the manifests of the ``enabled`` plugins. Raises ``PluginError``."""
    wanted = {name.strip() for name in enabled if name and name.strip()}
    if not wanted:
        return {}
    found: dict[str, PluginManifest] = {}
    seen: set[str] = set()
    for ep in _entry_points() if eps is None else eps:
        key = _matches(ep, wanted)
        if key is None:
            continue
        try:
            manifest = ep.load()
        except Exception as exc:
            raise PluginError(f"Plugin {ep.name!r} failed to load: {exc}") from exc
        if not isinstance(manifest, PluginManifest):
            raise PluginError(f"Entry point {ep.name!r} does not point to a PluginManifest.")
        if manifest.id in found:
            raise PluginError(f"Two plugins use the id {manifest.id!r}.")
        check_compatible(manifest)
        found[manifest.id] = manifest
        seen.add(key)
    missing = sorted(wanted - seen)
    if missing:
        raise PluginError(
            f"HOMEOA_PLUGINS lists {', '.join(missing)}, but no installed package provides "
            f"a {GROUP!r} entry point by that name."
        )
    return dict(sorted(found.items()))


def django_apps(enabled: Iterable[str], eps: Iterable[EntryPoint] | None = None) -> list[str]:
    """Django apps the enabled plugins need; settings use it to extend INSTALLED_APPS."""
    return [m.django_app for m in discover(enabled, eps).values() if m.django_app]


def load(enabled: Iterable[str], eps: Iterable[EntryPoint] | None = None) -> None:
    """Replace the registry with the given plugins (AppConfig.ready and tests)."""
    manifests = discover(enabled, eps)
    _loaded.clear()
    _loaded.update(manifests)


def register(manifest: PluginManifest) -> None:
    """Add a manifest directly (tests)."""
    check_compatible(manifest)
    _loaded[manifest.id] = manifest


def register_builtin(manifest: PluginManifest) -> None:
    """Add a core-shipped manifest (called from an app's ``ready``)."""
    check_compatible(manifest)
    _builtin[manifest.id] = manifest


def clear() -> None:
    _loaded.clear()


def get(plugin_id: str) -> PluginManifest | None:
    return _loaded.get(plugin_id) or _builtin.get(plugin_id)


def all_plugins() -> list[PluginManifest]:
    return [*_loaded.values(), *(m for k, m in _builtin.items() if k not in _loaded)]


def providers(interface: str) -> list[tuple[str, str]]:
    """``(plugin_id, dotted path)`` of every loaded plugin implementing ``interface``.

    Callers (provider resolution) must also check per-association enablement.
    """
    return [(m.id, m.providers[interface]) for m in _loaded.values() if interface in m.providers]


def _import_attr(path: str):
    module_path, _, attr = path.rpartition(".")
    return getattr(import_module(module_path), attr)


def load_modules(paths: Iterable[str]) -> dict[str, PluginManifest]:
    """Replace the module registry with the manifests at ``paths``. Raises ``PluginError``.

    Each path names a ``PluginManifest`` with ``kind="module"`` in the module's own file,
    so core never imports module code to find it.
    """
    found: dict[str, PluginManifest] = {}
    for path in paths:
        try:
            manifest = _import_attr(path)
        except Exception as exc:
            raise PluginError(f"Module manifest {path!r} failed to load: {exc}") from exc
        if not isinstance(manifest, PluginManifest) or manifest.kind != "module":
            raise PluginError(f"{path!r} is not a module manifest (kind='module').")
        if manifest.id in found:
            raise PluginError(f"Two modules use the id {manifest.id!r}.")
        found[manifest.id] = manifest
    for manifest in found.values():
        unknown = sorted(set(manifest.requires) - set(found))
        if unknown:
            raise PluginError(
                f"Module {manifest.id!r} requires unknown module(s): {', '.join(unknown)}."
            )
    _modules.clear()
    _modules.update(found)
    return dict(found)


def run_module_ready_hooks() -> None:
    """Call each loaded module's ``ready`` hook once, in load order."""
    for manifest in _modules.values():
        if manifest.ready:
            _import_attr(manifest.ready)()


def module(module_id: str) -> PluginManifest | None:
    return _modules.get(module_id)


def all_modules() -> list[PluginManifest]:
    return list(_modules.values())
