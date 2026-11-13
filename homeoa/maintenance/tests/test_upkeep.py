"""Preventive maintenance opens work orders before due dates; the component inventory."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.compliance import calendar as cal
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos
from homeoa.maintenance import services, upkeep
from homeoa.maintenance.models import Component, PreventiveTask, WorkOrder
from homeoa.plugins import registry as plugin_registry

User = get_user_model()
TODAY = dt.date(2026, 10, 7)
COMMITTEE = "maintenance@cedarrow.example"
OWNER = "ownerc@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def _gutters():
    return PreventiveTask.objects.get(title="Clean gutters and downspouts")


def test_due_task_opens_one_draft_work_order(pilot):
    opened = upkeep.open_due(pilot, TODAY)
    assert [wo.preventive_task for wo in opened] == [_gutters()]
    wo = opened[0]
    assert wo.status == WorkOrder.Status.DRAFT  # commissions nothing by itself
    assert wo.scheduled_for == dt.date(2026, 10, 15) and wo.component.name.startswith("Gutters")
    assert upkeep.open_due(pilot, TODAY) == []  # one open work order per task


def test_completing_schedules_the_next_date(pilot):
    (wo,) = upkeep.open_due(pilot, TODAY)
    services.set_work_order_status(wo, WorkOrder.Status.COMPLETED)
    task = _gutters()
    assert task.next_due == dt.date(2027, 4, 15) and task.last_done_on == TODAY


def test_cancelling_skips_to_the_next_date(pilot):
    (wo,) = upkeep.open_due(pilot, TODAY)
    services.set_work_order_status(wo, WorkOrder.Status.CANCELLED)
    task = _gutters()
    assert task.next_due == dt.date(2027, 4, 15) and task.last_done_on is None


def test_task_without_vendor_is_a_home_todo_until_marked_done(pilot):
    fence = PreventiveTask.objects.get(vendor__isnull=True)
    ids = [t.id for t in todos.collect(pilot)]
    assert f"maint-preventive-{fence.pk}-2026-10-20" in ids
    upkeep.mark_done(fence, on=TODAY)
    fence.refresh_from_db()
    assert fence.next_due == dt.date(2027, 10, 20)
    assert not [t for t in todos.collect(pilot) if t.id.startswith(f"maint-preventive-{fence.pk}")]


def test_months_clamp_to_the_end_of_month():
    assert upkeep.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)


def test_component_replacement_year(pilot):
    roof = Component.objects.get(name="Roof")
    assert roof.replace_year == 2033 and roof.remaining_years(TODAY) == 7


def test_tasks_on_the_compliance_calendar(pilot):
    rows = [e for e in cal.entries(pilot, TODAY, dt.date(2027, 6, 30)) if e.kind == "maintenance"]
    assert {e.due for e in rows} >= {dt.date(2026, 10, 15), dt.date(2027, 4, 15)}


def test_committee_manages_owners_cannot(pilot, client):
    client.force_login(User.objects.get(email=COMMITTEE))
    for name in ("maintenance:tasks", "maintenance:components"):
        assert client.get(reverse(name)).status_code == 200
    resp = client.post(
        reverse("maintenance:component_add"),
        {"name": "Dryer vents", "category": "other", "condition": "unknown", "active": "on"},
    )
    assert resp.status_code == 302 and Component.objects.filter(name="Dryer vents").exists()
    client.force_login(User.objects.get(email=OWNER))
    assert client.get(reverse("maintenance:tasks")).status_code in (302, 403)  # to /me/
