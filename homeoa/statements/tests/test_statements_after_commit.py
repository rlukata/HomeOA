"""A failing statement email never undoes generating or sending the statement."""

import logging

import pytest

from homeoa.audit.models import AuditEvent
from homeoa.notify.testing_alerts import notify_raises
from homeoa.statements import owner_statements as svc
from homeoa.statements.models import Statement

from .test_statement_sending import OCT, _consent, _on, pilot  # noqa: F401

pytestmark = pytest.mark.deferred_notify


def test_generate_and_send_stand_when_email_fails(
    pilot,  # noqa: F811
    monkeypatch,
    caplog,
    django_capture_on_commit_callbacks,
):
    _consent("C")
    with notify_raises(monkeypatch) as calls, caplog.at_level(logging.ERROR):
        with django_capture_on_commit_callbacks(execute=True):
            run = svc.generate(pilot, OCT)
            assert svc.NOTIFICATION_TYPE not in calls
        assert svc.NOTIFICATION_TYPE in calls
        c = run.statements.get(unit__number="C")
        assert c.channel == Statement.Channel.EMAIL
        _on(pilot)
        calls.clear()
        with django_capture_on_commit_callbacks(execute=True):
            svc.send_statement(c)
            assert svc.NOTIFICATION_TYPE not in calls
        assert svc.NOTIFICATION_TYPE in calls
    c.refresh_from_db()
    assert c.status == Statement.Status.SENT
    assert AuditEvent.objects.filter(action="statement.send", object_id=str(c.pk)).exists()
    assert AuditEvent.objects.filter(action="notify.send_failed").count() >= 2
    assert "failed after commit" in caplog.text
