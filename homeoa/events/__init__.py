"""In-process domain event bus (PLT-11, PLG-04 bus side).

Defining an event
-----------------
Subclass :class:`Event` as a frozen dataclass and give it a dotted ``name``. Every subclass
is added to the registry (``registry()``), which is what event docs are generated from::

    from dataclasses import dataclass
    from decimal import Decimal
    from homeoa.events import Event

    @dataclass(frozen=True, kw_only=True)
    class PaymentReceived(Event):
        name = "payment.received"
        unit_id: int
        amount: Decimal

Every event carries ``association_id`` and ``occurred_at`` (defaults to now, UTC).
Field values should be JSON-friendly or ``Decimal`` / ``date`` / ``datetime`` / ``UUID`` so
the event can be queued for job-mode handlers.

Subscribing
-----------
``subscribe(name, handler, mode=...)`` or the ``@on(name, mode=...)`` decorator. Modes:

* ``"sync"``: runs immediately inside the caller's transaction. An exception propagates and
  rolls the transaction back, so use it for work that must succeed together.
* ``"on_commit"``: runs after the surrounding transaction commits (immediately when there is
  no transaction). Never runs on rollback. Exceptions are logged and swallowed.
* ``"job"``: enqueues a Procrastinate job in the same transaction (so a rollback discards
  it). The worker runs the handler with the reconstructed event. The handler must be a
  module-level function (it is located by its dotted path).

``name="*"`` subscribes to every event. Register subscriptions in ``AppConfig.ready``.

``handle(name, handler, mode=..., module=...)`` adds the handler to the event handlers slot
instead, tagged with the module that owns it (``None`` for core). Optional modules register
their handlers this way from their ``ready`` hook; they run after the plain subscribers.

Publishing
----------
``publish(event)`` dispatches to every subscriber in subscription order.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

Mode = Literal["sync", "on_commit", "job"]
MODES: tuple[str, ...] = ("sync", "on_commit", "job")
Handler = Callable[["Event"], Any]

_event_types: dict[str, type[Event]] = {}
_subscribers: list[tuple[str, Handler, str]] = []


@dataclasses.dataclass(frozen=True, kw_only=True)
class Event:
    """Base for every domain event. Subclasses set the ``name`` class attribute."""

    name: ClassVar[str] = ""

    association_id: int
    occurred_at: dt.datetime = dataclasses.field(default_factory=timezone.now)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            raise TypeError(f"{cls.__qualname__} must set a 'name' such as 'payment.received'.")
        existing = _event_types.get(cls.name)
        if existing is not None and existing.__qualname__ != cls.__qualname__:
            raise ValueError(f"Event name {cls.name!r} is already used by {existing!r}.")
        _event_types[cls.name] = cls

    # Serialization, used by job-mode handlers.
    def to_payload(self) -> dict[str, Any]:
        return {f.name: _encode(getattr(self, f.name)) for f in dataclasses.fields(self)}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Event:
        return cls(**{k: _decode(v) for k, v in payload.items()})


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__t": "decimal", "v": str(value)}
    if isinstance(value, dt.datetime):
        return {"__t": "datetime", "v": value.isoformat()}
    if isinstance(value, dt.date):
        return {"__t": "date", "v": value.isoformat()}
    if isinstance(value, UUID):
        return {"__t": "uuid", "v": str(value)}
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


_DECODERS = {
    "decimal": Decimal,
    "datetime": dt.datetime.fromisoformat,
    "date": dt.date.fromisoformat,
    "uuid": UUID,
}


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__t", "v"} and value["__t"] in _DECODERS:
            return _DECODERS[value["__t"]](value["v"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def registry() -> dict[str, type[Event]]:
    """Every known event type by name (sorted), for documentation and plugin manifests."""
    return dict(sorted(_event_types.items()))


def get_event_type(name: str) -> type[Event]:
    return _event_types[name]


def _check_mode(handler: Handler, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode!r}; use one of {MODES}.")
    if mode == "job":
        path = handler_path(handler)
        if "<locals>" in path:
            raise ValueError("Job-mode handlers must be module-level functions.")


def subscribe(name: str, handler: Handler, mode: Mode = "sync") -> Handler:
    _check_mode(handler, mode)
    if (name, handler, mode) not in _subscribers:
        _subscribers.append((name, handler, mode))
    return handler


def on(name: str, mode: Mode = "sync") -> Callable[[Handler], Handler]:
    """Decorator form of :func:`subscribe`."""

    def decorator(handler: Handler) -> Handler:
        return subscribe(name, handler, mode)

    return decorator


def unsubscribe(name: str, handler: Handler, mode: Mode | None = None) -> None:
    _subscribers[:] = [
        s for s in _subscribers if not (s[0] == name and s[1] == handler and mode in (None, s[2]))
    ]


def handle(
    name: str, handler: Handler, mode: Mode = "sync", *, module: str | None = None
) -> Handler:
    """Add ``handler`` for ``name`` to the event handlers slot on behalf of ``module``."""
    from homeoa.plugins import slots

    _check_mode(handler, mode)
    slots.register(slots.EVENT_HANDLERS, (name, handler, mode), module=module)
    return handler


def subscribers(name: str, association_id: int | None = None) -> list[tuple[Handler, str]]:
    """Plain subscribers to ``name``, then the slot's handlers for it, in order. A module's
    handlers are left out when the module is off for ``association_id``."""
    from homeoa.plugins import slots

    found = [(h, m) for n, h, m in _subscribers if n in (name, "*")]
    mine = [c for c in slots.contributions(slots.EVENT_HANDLERS) if c.value[0] in (name, "*")]
    if association_id is not None and any(c.module for c in mine):
        mine = slots.contributions(slots.EVENT_HANDLERS, association_id)
        mine = [c for c in mine if c.value[0] in (name, "*")]
    found.extend((c.value[1], c.value[2]) for c in mine)
    return found


def handler_path(handler: Handler) -> str:
    return f"{handler.__module__}.{handler.__qualname__}"


def publish(event: Event) -> None:
    """Dispatch ``event`` to its subscribers. Sync handler errors propagate."""
    if not isinstance(event, Event) or not type(event).name:
        raise TypeError("publish() takes an Event subclass instance.")
    for handler, mode in subscribers(event.name, event.association_id):
        if mode == "sync":
            handler(event)
        elif mode == "on_commit":
            transaction.on_commit(_isolated(handler, event))
        else:
            from homeoa.events.tasks import run_event_handler

            run_event_handler.defer(
                handler=handler_path(handler), event=event.name, payload=event.to_payload()
            )


def _isolated(handler: Handler, event: Event) -> Callable[[], None]:
    def run() -> None:
        try:
            handler(event)
        except Exception:
            logger.exception("on_commit handler %s failed for %s", handler_path(handler), event)

    return run


def run_job_handler(handler: str, event: str, payload: dict[str, Any]) -> None:
    """Worker side of job mode: rebuild the event and call the handler."""
    import_string(handler)(get_event_type(event).from_payload(payload))
