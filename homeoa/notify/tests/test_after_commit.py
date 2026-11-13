"""Notify_after_commit waits for commit and never raises."""

import logging

import pytest

from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.notify import services
from homeoa.notify.testing_alerts import notify_raises

pytestmark = pytest.mark.deferred_notify


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def test_runs_only_on_commit(assoc, monkeypatch, django_capture_on_commit_callbacks):
    seen = []
    monkeypatch.setattr(services, "notify", lambda *a, **k: seen.append(a[0]) or [])
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        services.notify_after_commit("x.type", [], {}, association=assoc)
        assert seen == []
    assert len(callbacks) == 1 and seen == ["x.type"]


def test_failure_is_logged_and_audited(
    assoc, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    with (
        notify_raises(monkeypatch) as calls,
        caplog.at_level(logging.ERROR),
        django_capture_on_commit_callbacks(execute=True),
    ):
        services.notify_after_commit("x.type", [], {}, association=assoc, obj=assoc)
    assert calls == ["x.type"]
    event = AuditEvent.objects.get(action="notify.send_failed")
    assert "x.type" in event.message and "backend down" in event.message
    assert "x.type failed after commit" in caplog.text
