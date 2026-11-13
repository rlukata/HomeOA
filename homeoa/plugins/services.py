"""Per-association plugin state and event-handler wiring. See ``homeoa.plugins``."""

from __future__ import annotations

import re
import sys
from typing import Any

from django.utils.module_loading import import_string
from pydantic import ValidationError

from homeoa import events

from . import registry
from .manifest import PluginManifest
from .models import PluginInstallation

# Synthetic module-level wrappers for job-mode plugin handlers live on this module so the
# worker can import them by dotted path (events job mode requires module-level callables).
_handlers_module = sys.modules[__name__]


class SettingsError(Exception):
    """Settings failed validation. ``errors`` maps field name (or ``""``) to messages."""

    def __init__(self, errors: dict[str, list[str]]):
        super().__init__(str(errors))
        self.errors = errors


def installation_for(association, plugin_id: str) -> PluginInstallation | None:
    return PluginInstallation.all_associations.filter(
        association=association, plugin_id=plugin_id
    ).first()


def is_enabled(association_id: int, plugin_id: str) -> bool:
    if registry.get(plugin_id) is None:
        return False
    return PluginInstallation.all_associations.filter(
        association_id=association_id, plugin_id=plugin_id, enabled=True
    ).exists()


def validate_settings(
    manifest: PluginManifest, data: dict[str, Any], current_secrets: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate ``data`` against the manifest's schema.

    Secret fields left empty keep their current value. Returns ``(settings, secrets)`` as
    JSON-ready dicts. Raises ``SettingsError``.
    """
    model = manifest.settings_model
    if model is None:
        return {}, {}
    secrets = dict(current_secrets or {})
    merged = {}
    for name in model.model_fields:
        value = data.get(name)
        if name in manifest.secret_fields and value in (None, ""):
            if name in secrets:
                merged[name] = secrets[name]
            continue
        if value is not None:
            merged[name] = value
    try:
        validated = model.model_validate(merged).model_dump(mode="json")
    except ValidationError as exc:
        errors: dict[str, list[str]] = {}
        for err in exc.errors():
            key = str(err["loc"][0]) if err["loc"] else ""
            errors.setdefault(key, []).append(err["msg"])
        raise SettingsError(errors) from exc
    settings = {k: v for k, v in validated.items() if k not in manifest.secret_fields}
    new_secrets = {k: v for k, v in validated.items() if k in manifest.secret_fields}
    return settings, new_secrets


def save_installation(
    association, plugin_id: str, *, enabled: bool, data: dict[str, Any] | None = None
) -> PluginInstallation:
    """Create or update an installation. Validates settings; audited via ``homeoa.audit``."""
    manifest = registry.get(plugin_id)
    if manifest is None:
        raise LookupError(plugin_id)
    inst = installation_for(association, plugin_id) or PluginInstallation(
        association=association, plugin_id=plugin_id
    )
    if data is not None:
        inst.settings, inst.secrets = validate_settings(manifest, data, inst.secrets)
    elif enabled and manifest.settings_model is not None:
        # Enabling requires settings that pass the schema.
        validate_settings(manifest, inst.settings, inst.secrets)
    inst.enabled = enabled
    inst.save()
    return inst


def _gated(manifest: PluginManifest, handler, target: str):
    def run(event):
        if is_enabled(event.association_id, manifest.id):
            return handler(event)
        return None

    run.__name__ = run.__qualname__ = "_h_" + re.sub(r"\W", "_", f"{manifest.id}__{target}")
    run.__module__ = __name__
    run.plugin_id = manifest.id
    return run


_wired: list[tuple[str, Any, str]] = []


def wire_event_handlers() -> None:
    """Subscribe every loaded plugin's handlers, gated by per-association enablement."""
    unwire_event_handlers()
    for manifest in registry.all_plugins():
        for spec in manifest.event_handlers:
            handler = import_string(spec.handler)
            wrapper = _gated(manifest, handler, spec.handler)
            setattr(_handlers_module, wrapper.__name__, wrapper)
            events.subscribe(spec.event, wrapper, spec.mode)
            _wired.append((spec.event, wrapper, spec.mode))


def unwire_event_handlers() -> None:
    for name, wrapper, mode in _wired:
        events.unsubscribe(name, wrapper, mode)
        if getattr(_handlers_module, wrapper.__name__, None) is wrapper:
            delattr(_handlers_module, wrapper.__name__)
    _wired.clear()
