"""Repo-wide pytest fixtures (unit tests; e2e/ has its own conftest)."""

import pytest


@pytest.fixture(autouse=True)
def _restore_plugin_registry():
    """Put the plugin registry back after every test.

    Many plugin/provider tests ``register`` a fake and ``registry.clear()`` in teardown,
    which also dropped the plugins loaded from settings (simplefin, lob, stripe) for every
    later test. Snapshot and restore instead.
    """
    from homeoa.plugins import registry

    loaded = dict(registry._loaded)
    builtin = dict(registry._builtin)
    yield
    registry._loaded.clear()
    registry._loaded.update(loaded)
    registry._builtin.clear()
    registry._builtin.update(builtin)


@pytest.fixture(autouse=True)
def _restore_environ():
    """Put ``os.environ`` back after every test.

    Tests that reload a settings module re-run ``base._load_dotenv``, which copied the
    checkout's real ``.env`` (e.g. the one-time SimpleFIN demo token that ``test.py`` blanks)
    back into the environment for every later test. Snapshot and restore instead.
    """
    import os

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _notify_after_commit_at_once(request, monkeypatch):
    """Unit tests run inside one transaction that never commits, so by default
    ``notify_after_commit`` runs its (savepointed, never-raising) send at once. Tests of the
    after-commit alert ordering itself mark ``deferred_notify`` and capture on_commit callbacks."""
    from django.conf import settings

    # E2E runs without Django in the test process (-p no:django); nothing to patch there.
    if not settings.configured or request.node.get_closest_marker("deferred_notify"):
        return
    from homeoa.notify import services

    monkeypatch.setattr(services, "_schedule", lambda fn: fn())
