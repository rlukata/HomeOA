import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import pytest
from django.db import transaction

from homeoa import events
from homeoa.core.models import Association
from homeoa.events import Event, publish, subscribe, unsubscribe
from homeoa.jobs.testing import capture_jobs
from tests.scoped.models import Widget


@dataclass(frozen=True, kw_only=True)
class WidgetSold(Event):
    name = "test.widget_sold"
    widget: str
    amount: Decimal
    sold_on: dt.date


JOB_CALLS: list = []


def job_handler(event):
    JOB_CALLS.append(event)


@pytest.fixture
def assoc(db):
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def subs():
    added = []

    def add(handler, mode, name="test.widget_sold"):
        subscribe(name, handler, mode)
        added.append((name, handler))

    yield add
    for name, handler in added:
        unsubscribe(name, handler)


def make(assoc, widget="w1"):
    return WidgetSold(
        association_id=assoc.pk, widget=widget, amount=Decimal("10.50"), sold_on=dt.date(2026, 9, 1)
    )


def test_registry_and_base_fields(assoc):
    assert events.registry()["test.widget_sold"] is WidgetSold
    ev = make(assoc)
    assert ev.occurred_at.tzinfo is not None


def test_event_requires_name():
    with pytest.raises(TypeError):

        @dataclass(frozen=True, kw_only=True)
        class Nameless(Event):
            pass


def test_duplicate_name_rejected():
    with pytest.raises(ValueError):

        @dataclass(frozen=True, kw_only=True)
        class Other(Event):
            name = "test.widget_sold"


def test_payload_round_trip(assoc):
    ev = make(assoc)
    assert WidgetSold.from_payload(ev.to_payload()) == ev


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        subscribe("x", job_handler, "later")


def test_job_mode_rejects_local_function():
    def local(event):
        pass

    with pytest.raises(ValueError):
        subscribe("x", local, "job")


def test_sync_runs_in_transaction_and_rolls_back(assoc, subs):
    def handler(event):
        Widget.all_associations.create(association_id=event.association_id, name=event.widget)

    subs(handler, "sync")
    with pytest.raises(RuntimeError), transaction.atomic():
        publish(make(assoc, "rolled"))
        assert Widget.all_associations.filter(name="rolled").exists()
        raise RuntimeError
    assert not Widget.all_associations.filter(name="rolled").exists()
    publish(make(assoc, "kept"))
    assert Widget.all_associations.filter(name="kept").exists()


def test_sync_error_propagates(assoc, subs):
    def boom(event):
        raise ValueError("no")

    subs(boom, "sync")
    with pytest.raises(ValueError):
        publish(make(assoc))


def test_on_commit_runs_after_commit_only(assoc, subs, django_capture_on_commit_callbacks):
    seen = []
    subs(lambda e: seen.append(e.widget), "on_commit")
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            publish(make(assoc, "rolled"))
            raise RuntimeError
        publish(make(assoc, "committed"))
        assert seen == []
    assert seen == ["committed"]


def test_on_commit_failure_is_isolated(assoc, subs, django_capture_on_commit_callbacks, caplog):
    seen = []

    def boom(event):
        raise ValueError("handler broke")

    subs(boom, "on_commit")
    subs(lambda e: seen.append(e.widget), "on_commit")
    with django_capture_on_commit_callbacks(execute=True):
        publish(make(assoc, "w"))
    assert seen == ["w"]
    assert "handler broke" in caplog.text


def test_wildcard_subscriber(assoc, subs):
    seen = []
    subs(lambda e: seen.append(e.name), "sync", name="*")
    publish(make(assoc))
    assert seen == ["test.widget_sold"]


def test_job_mode_enqueues_and_runs(assoc, subs):
    JOB_CALLS.clear()
    subs(job_handler, "job")
    with capture_jobs() as queue:
        publish(make(assoc))
        assert JOB_CALLS == []
        assert queue.pending_names() == ["homeoa.events.run_event_handler"]
        assert queue.run_all() == 1
    assert [make(assoc).__class__.from_payload(JOB_CALLS[0].to_payload())] == JOB_CALLS
    assert JOB_CALLS[0].amount == Decimal("10.50")
    assert JOB_CALLS[0].sold_on == dt.date(2026, 9, 1)


def test_job_mode_enqueue_is_transactional(assoc, subs):
    """With the real Django connector, a rolled-back transaction discards the job."""
    from procrastinate.contrib.django.models import ProcrastinateJob

    subs(job_handler, "job")
    with pytest.raises(RuntimeError), transaction.atomic():
        publish(make(assoc))
        assert ProcrastinateJob.objects.count() == 1
        raise RuntimeError
    assert ProcrastinateJob.objects.count() == 0


def test_publish_rejects_non_event():
    with pytest.raises(TypeError):
        publish("payment.received")


def test_a_module_handler_registered_through_the_slot_runs_on_publish_and_is_tagged(db):
    from homeoa.plugins import slots

    saved = {k: list(v) for k, v in slots._contributions.items()}
    seen = []
    try:
        events.handle("test.widget_sold", seen.append, module="announcements")
        event = WidgetSold(
            association_id=1, widget="w", amount=Decimal("1.00"), sold_on=dt.date(2026, 1, 1)
        )
        publish(event)
        assert seen == [event]
        [tagged] = [
            c for c in slots.contributions(slots.EVENT_HANDLERS) if c.value[0] == "test.widget_sold"
        ]
        assert tagged.module == "announcements"
    finally:
        slots._contributions.clear()
        slots._contributions.update(saved)


def test_the_board_packet_handles_month_closes_through_the_slot():
    from homeoa.plugins import slots

    handlers = [
        c for c in slots.contributions(slots.EVENT_HANDLERS) if c.value[0] == "closing.month_closed"
    ]
    assert [(c.module, c.value[2]) for c in handlers] == [("board_packet", "on_commit")]
