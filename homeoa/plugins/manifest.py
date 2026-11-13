"""The ``PluginManifest`` a plugin exposes through the ``homeoa.plugins`` entry point.

This module must stay importable before Django is configured: settings read manifests to
add plugin Django apps to ``INSTALLED_APPS``. Plugin manifest modules follow the same rule
(no model imports at module level); everything else is referenced by dotted path.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Literal

# Keep in step with ``[project] version`` in pyproject.toml (a test checks this).
CORE_VERSION = "0.1.0"

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,49}$")

Mode = Literal["sync", "on_commit", "job"]
Kind = Literal["plugin", "module"]


@dataclasses.dataclass(frozen=True)
class EventHandler:
    """Subscribe ``handler`` (dotted path to a module-level callable) to ``event``.

    The handler only runs for associations where the plugin is enabled.
    """

    event: str
    handler: str
    mode: Mode = "sync"


@dataclasses.dataclass(frozen=True)
class PluginManifest:
    """What a plugin declares about itself.

    ``id``             stable identifier, ``[a-z][a-z0-9_]+``; used in URLs and DB rows.
    ``name``           human-readable name (may be a lazy translation).
    ``version``        the plugin's own version string.
    ``core_requires``  PEP 440 specifier of compatible core versions, e.g. ``">=0.1,<0.2"``.
    ``settings_model`` pydantic ``BaseModel`` subclass for per-association settings, or None.
    ``secret_fields``  names of ``settings_model`` fields that are secrets: stored encrypted,
                       never shown again after save.
    ``providers``      provider interface name -> dotted path of the implementation.
    ``event_handlers`` ``EventHandler`` entries.
    ``webhook``        dotted path to an object or class implementing ``WebhookHandler``.
    ``django_app``     dotted app path added to ``INSTALLED_APPS`` (for models/migrations).
    ``urls``           dotted URLconf module mounted at ``/plugins/<id>/`` (needs ``app_name``).
    ``description``    short text for the plugin list.
    ``todos``          dotted path to a Home to-do provider ``fn(association) -> list[dict]``
                       (see ``homeoa.dashboard.todos``); used while the plugin is enabled.
    ``kind``           ``"plugin"`` (an installed package) or ``"module"`` (an optional
                       feature shipped inside HomeOA, listed in ``HOMEOA_MODULES``).
    ``requires``       ids of other modules this module needs switched on.
    ``ready``          dotted path to ``fn()`` called once at startup, after apps are ready;
                       a module registers its slot contributions there.
    ``code``           the module's own Python code, as paths relative to the repository
                       root: a package (``"homeoa/enforcement"``) or file globs
                       (``"homeoa/registry/directory*.py"``). Core may not import it; an
                       app's ``models.py`` and migrations always stay core.
    ``pages``          the module's pages as URL names, ``"namespace:name"`` with shell-style
                       wildcards (``"enforcement:*"``). They return 404 while the module is
                       off; a page several modules list is shown while any of them is on.
    """

    id: str
    name: Any
    version: str
    core_requires: str = ""
    settings_model: type | None = None
    secret_fields: tuple[str, ...] = ()
    providers: dict[str, str] = dataclasses.field(default_factory=dict)
    event_handlers: tuple[EventHandler, ...] = ()
    webhook: str | None = None
    django_app: str | None = None
    urls: str | None = None
    description: Any = ""
    todos: str | None = None
    kind: Kind = "plugin"
    requires: tuple[str, ...] = ()
    ready: str | None = None
    code: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()

    def __post_init__(self):
        if not PLUGIN_ID_RE.match(self.id):
            raise ValueError(f"Plugin id {self.id!r} must match {PLUGIN_ID_RE.pattern}.")
        if self.secret_fields:
            fields = getattr(self.settings_model, "model_fields", {})
            missing = [f for f in self.secret_fields if f not in fields]
            if missing:
                raise ValueError(
                    f"Plugin {self.id}: secret_fields not in settings_model: {missing}"
                )


class WebhookHandler:
    """Protocol for ``PluginManifest.webhook``. Subclass it or duck-type it.

    ``installation`` is the association's ``PluginInstallation``; read ``.settings`` and
    ``.secrets`` (a decrypted dict) from it.
    """

    def verify_signature(self, request, installation) -> bool:
        raise NotImplementedError

    def event_id(self, request) -> str:
        """A stable id for the delivery; repeats are acknowledged but not processed again."""
        raise NotImplementedError

    def handle(self, request, installation) -> None:
        """Process a verified, first-seen delivery. Runs inside a transaction."""
        raise NotImplementedError
