"""Home to-do registry (TodoProvider extension point).

A *provider* is a callable ``provider(association) -> list[dict]``. Each dict is shaped like
the prototype's to-dos: ``id`` (stable, unique), ``kind`` (``money``, ``requests``,
``deadline``, ``enforce``, ``gov``: the icon colour), ``title``, ``sub``, ``url`` and
``action`` (button label), optionally ``due`` (a date) and ``icon``. An item may carry
``post_url`` and ``post_data`` for a one-click action: the Home page posts it through
``dashboard:todo_act`` (which only forwards actions of items the provider still lists).
An item may carry ``for_user`` (a user pk): it is then personal and ``collect(association,
user)`` shows it only to that user (e.g. "Approve bill" only to that approver).

Providers come from four places:

* the ``HOME_TODOS`` slot: core providers (``CORE_PROVIDERS``, registered when the
  dashboard app is ready) and optional modules' providers, each a ``(name, provider)`` pair
  whose provider may be a dotted path (a missing one is skipped). Core providers sit at
  ``CORE_ORDER_STEP`` steps, so a module picks an ``order`` between two of them;
* plugins: ``PluginManifest.todos`` names one provider, used while the plugin is enabled
  for the association;
* ``register(name, provider)`` at runtime (tests, other apps).

The no-plugin default is simply the core list. A failing provider is logged and skipped so
one broken app never blanks the Home page.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

logger = logging.getLogger(__name__)

TodoProvider = Callable[[object], list[dict]]

CORE_PROVIDERS: dict[str, str] = {
    "banking": "homeoa.banking.todos.todo_items",
    "ops": "homeoa.dashboard.todos.backup_items",
    "parallel_run": "homeoa.imports.diff.todos.todo_items",
    "notify": "homeoa.notify.todos.todo_items",
    "delinquency": "homeoa.statements.delinquency.todo_items",
    "interest": "homeoa.billing.services.latefees.todo_items",
    "compliance": "homeoa.compliance.services.todo_items",
    "vendors": "homeoa.vendors.alerts.todo_items",
    "bills": "homeoa.ap.todos.todo_items",
    "changes": "homeoa.changes.todos.todo_items",
    "interfund": "homeoa.ledger.interfund.todo_items",
    "reserve": "homeoa.banking.reserve.todo_items",
    "setup": "homeoa.setup.services.todo_items",
    "records": "homeoa.registry.records.todo_items",
    "resale": "homeoa.resale.services.todo_items",
    "closing": "homeoa.closing.todos.todo_items",
    "governance": "homeoa.governance.todos.todo_items",
    "votes": "homeoa.votes.todos.todo_items",
    "meetings": "homeoa.meetings.todos.todo_items",
    "annual_meeting": "homeoa.meetings.annual.todo_items",
    "esign": "homeoa.esign.todos.todo_items",
    "law_check": "homeoa.rules.lawcheck.todo_items",
}

CORE_ORDER_STEP = 10

_registered: dict[str, TodoProvider] = {}


def register_core_providers() -> None:
    from homeoa.plugins import slots

    for index, pair in enumerate(CORE_PROVIDERS.items()):
        slots.register(slots.HOME_TODOS, pair, order=index * CORE_ORDER_STEP)


GROUPS = (
    ("today", gettext_lazy("Today")),
    ("week", gettext_lazy("This week")),
    ("upcoming", gettext_lazy("Upcoming")),
)

KIND_ICONS = {
    "money": "money",
    "requests": "wrench",
    "deadline": "clock",
    "enforce": "flag",
    "gov": "users",
    "ops": "alert",
}


@dataclass
class TodoItem:
    id: str
    title: str
    kind: str = "money"
    sub: str = ""
    url: str = ""
    action: str = ""
    due: dt.date | None = None
    icon: str = ""
    post_url: str = ""
    post_data: dict = field(default_factory=dict)
    source: str = ""
    for_user: int | None = None
    perm: str = ""

    @classmethod
    def from_dict(cls, data: dict, source: str = "") -> TodoItem:
        kind = str(data.get("kind") or "money")
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            kind=kind,
            sub=str(data.get("sub", "")),
            url=str(data.get("url", "")),
            action=str(data.get("action", "")),
            due=data.get("due"),
            icon=str(data.get("icon") or KIND_ICONS.get(kind, "alert")),
            post_url=str(data.get("post_url", "")),
            post_data=dict(data.get("post_data") or {}),
            source=source,
            for_user=int(data["for_user"]) if data.get("for_user") is not None else None,
            perm=str(data.get("perm") or ""),
        )

    @property
    def dom_id(self) -> str:
        return "todo-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in self.id)


def register(name: str, provider: TodoProvider) -> None:
    _registered[name] = provider


def unregister(name: str) -> None:
    _registered.pop(name, None)


def providers(association) -> list[tuple[str, TodoProvider]]:
    """``(name, provider)`` pairs that apply to ``association``, in a stable order."""
    out: list[tuple[str, TodoProvider]] = []
    from homeoa.plugins import registry, services, slots

    for name, provider in slots.values(slots.HOME_TODOS, association):
        if isinstance(provider, str):
            try:
                provider = import_string(provider)
            except ImportError:
                logger.debug("to-do provider %s not available", provider)
                continue
        out.append((name, provider))

    for manifest in registry.all_plugins():
        path = getattr(manifest, "todos", None)
        if not path or association is None or not services.is_enabled(association.pk, manifest.id):
            continue
        try:
            out.append((f"plugin:{manifest.id}", import_string(path)))
        except ImportError:
            logger.warning("plugin %s to-do provider %s not importable", manifest.id, path)
    out.extend(sorted(_registered.items()))
    return out


def collect(association, user=None) -> list[TodoItem]:
    """Every provider's items, de-duplicated by id, sorted by due date (undated first).

    With ``user``, personal items (``for_user``) addressed to someone else are left out.
    Without it every item is returned (system callers, tests).
    """
    if association is None:
        return []
    from homeoa.access.services import has_perm

    items: dict[str, TodoItem] = {}
    for name, provider in providers(association):
        try:
            raw = provider(association) or []
        except Exception:
            logger.exception("to-do provider %s failed", name)
            continue
        for data in raw:
            try:
                item = TodoItem.from_dict(data, source=name)
            except KeyError, TypeError, ValueError:
                logger.warning("to-do provider %s returned a malformed item", name)
                continue
            if user is not None and item.for_user is not None and item.for_user != user.pk:
                continue
            if (
                user is not None
                and item.perm
                and not has_perm(user, item.perm, association=association)
            ):
                continue
            items.setdefault(item.id, item)
    return sorted(items.values(), key=lambda i: (i.due or dt.date.min, i.title))


def group_of(item: TodoItem, today: dt.date) -> str:
    """Overdue, due today or undated: Today; within 7 days: This week; later: Upcoming."""
    if item.due is None or item.due <= today:
        return "today"
    if item.due <= today + dt.timedelta(days=6):
        return "week"
    return "upcoming"


def grouped(items: list[TodoItem], today: dt.date) -> list[dict]:
    buckets: dict[str, list[TodoItem]] = {key: [] for key, _label in GROUPS}
    for item in items:
        buckets[group_of(item, today)].append(item)
    return [{"key": k, "label": label, "items": buckets[k]} for k, label in GROUPS]


# ---------------------------------------------------------------- core: backups


def backup_items(association) -> list[dict]:
    """``ops.backup_stale`` as a board to-do while backups are old, missing or failing."""
    from homeoa.core import ops

    status = ops.backup_status()
    if status is None or not status.stale:
        return []
    from django.conf import settings

    hours = getattr(settings, "HOMEOA_BACKUP_MAX_AGE_HOURS", 36)
    return [
        {
            "id": "ops-backup-stale",
            "kind": "enforce",
            "icon": "alert",
            "title": _("Backups need attention"),
            "sub": _(ops._MESSAGES[status.reason]) % {"hours": hours},
            "url": "",
            "action": "",
        }
    ]
