"""A failing delinquency email copy never undoes the recorded step."""

import logging

import pytest

from homeoa.audit.models import AuditEvent
from homeoa.notify.testing_alerts import notify_raises
from homeoa.statements import delinquency
from homeoa.statements.models import DelinquencyAction

from .test_delinquency import assoc, consent, day, steps  # noqa: F401

pytestmark = pytest.mark.deferred_notify


def test_step_stands_when_email_copy_fails(
    assoc,  # noqa: F811
    monkeypatch,
    caplog,
    django_capture_on_commit_callbacks,
):
    consent("Q")
    with (
        notify_raises(monkeypatch) as calls,
        caplog.at_level(logging.ERROR),
        django_capture_on_commit_callbacks(execute=True),
    ):
        delinquency.run(assoc, day(15))
        assert calls == []  # nothing is sent inside the transaction
    assert calls  # sent after commit, and it failed
    assert steps("Q") == ["reminder"]
    assert DelinquencyAction.objects.filter(case__unit__number="Q").exists()
    assert AuditEvent.objects.filter(action="notify.send_failed").exists()
    assert AuditEvent.objects.filter(action="delinquency.step").exists()
    assert "failed after commit" in caplog.text
