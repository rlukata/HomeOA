"""Test helper for after-commit alerts: make every ``notify`` call raise, as a broken
channel or template would, so a test can check the caller's state change still stands."""

from __future__ import annotations

import contextlib


class Boom(RuntimeError):
    pass


@contextlib.contextmanager
def notify_raises(monkeypatch):
    from homeoa.notify import services

    calls = []

    def boom(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("type_key"))
        raise Boom("notification backend down")

    monkeypatch.setattr(services, "notify", boom)
    yield calls
