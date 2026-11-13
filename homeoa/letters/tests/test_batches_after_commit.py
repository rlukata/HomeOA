"""A failing letters.mailed notice never undoes creating the mail batch."""

import logging

import pytest

from homeoa.audit.models import AuditEvent
from homeoa.letters import batches
from homeoa.letters.models import MailBatch
from homeoa.notify.testing_alerts import notify_raises

from .test_batches import assoc, letters  # noqa: F401

pytestmark = pytest.mark.deferred_notify


def test_batch_stands_when_notice_fails(
    assoc,  # noqa: F811
    letters,  # noqa: F811
    monkeypatch,
    caplog,
    django_capture_on_commit_callbacks,
):
    with (
        notify_raises(monkeypatch) as calls,
        caplog.at_level(logging.ERROR),
        django_capture_on_commit_callbacks(execute=True),
    ):
        batch = batches.create_batch(assoc, letters, name="October")
        assert calls == []
    assert calls == [batches.NOTIFICATION_TYPE] * 2
    batch = MailBatch.objects.get(pk=batch.pk)
    assert batch.items.count() == 2
    assert AuditEvent.objects.filter(action="mail_batch.create").exists()
    assert AuditEvent.objects.filter(action="notify.send_failed").count() == 2
    assert "failed after commit" in caplog.text


def test_postal_deliveries_linked_after_commit(
    assoc,  # noqa: F811
    letters,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        batch = batches.create_batch(assoc, letters, name="October")
    assert all(item.deliveries.count() == 1 for item in batch.items.all())
