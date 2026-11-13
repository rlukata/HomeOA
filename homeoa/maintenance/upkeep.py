"""Preventive maintenance (MNT-03) and the component inventory (MNT-04).

A daily job opens a *draft* work order for each active task that is within its lead time
and has a vendor; drafts commission nothing, so the one-off spending limit still
applies when the committee sends it. Completing or cancelling that work order moves the
task to its next date (counted from the due date, so it never drifts). A task with no
vendor shows on Home instead.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _

from .models import Component, ComponentPhoto, PreventiveTask, WorkOrder
from .preventive import add_months, opens_on


def _open_work_order(task: PreventiveTask) -> WorkOrder | None:
    return task.work_orders.filter(status__in=WorkOrder.OPEN).first()


@transaction.atomic
def save_task(task: PreventiveTask) -> PreventiveTask:
    task.full_clean()
    task.save()
    return task


@transaction.atomic
def save_component(component: Component) -> Component:
    component.full_clean()
    component.save()
    return component


@transaction.atomic
def add_photo(component: Component, *, filename: str, content: bytes, user=None):
    from homeoa.documents import services as documents
    from homeoa.documents.models import Category, Visibility

    documents.check_photo(filename, len(content))
    version = documents.attach(
        component.association,
        filename=filename,
        content=content,
        title=_("Component photo: %(name)s") % {"name": component.name},
        category=Category.OTHER,
        visibility=Visibility.BOARD,
        user=user,
    )
    return ComponentPhoto.all_associations.create(
        association=component.association, component=component, document=version.document
    )


def open_due(association, as_of: dt.date) -> list[WorkOrder]:
    """Open a draft work order for every task that is due soon (idempotent)."""
    from .services import create_work_order

    out = []
    tasks = PreventiveTask.all_associations.filter(
        association=association, active=True, vendor__isnull=False
    ).select_related("vendor", "component")
    for task in tasks:
        if opens_on(task) > as_of or _open_work_order(task) is not None:
            continue
        scope = task.title + (f"\n\n{task.scope}" if task.scope else "")
        wo = create_work_order(
            vendor=task.vendor,
            scope=scope,
            estimated_cost=task.estimated_cost,
            scheduled_for=task.next_due,
        )
        wo.preventive_task = task
        wo.component = task.component
        wo.save(update_fields=["preventive_task", "component"])
        out.append(wo)
    return out


@transaction.atomic
def mark_done(task: PreventiveTask, *, on: dt.date) -> PreventiveTask:
    """Done without a work order (a volunteer did it): schedule the next date."""
    task.last_done_on = on
    task.next_due = add_months(task.next_due, task.every_months)
    task.save(update_fields=["last_done_on", "next_due"])
    return task


def calendar_entries(association, start: dt.date, end: dt.date) -> list[tuple]:
    """``(task, due date)`` for the compliance calendar (read-only rows)."""
    out = []
    for task in PreventiveTask.all_associations.filter(association=association, active=True):
        day = task.next_due
        while day <= end:
            if day >= start:
                out.append((task, day))
            day = add_months(day, task.every_months)
    return out


def compliance_entries(association, start: dt.date, end: dt.date) -> list:
    """Preventive maintenance dates on the compliance calendar (read-only there; their work
    orders are the reminders)."""
    from homeoa.compliance.calendar import Entry

    return [
        Entry(
            uid=f"compliance-upkeep-{task.pk}-{day.isoformat()}.a{association.pk}@homeoa",
            kind="maintenance",
            title=_("Preventive maintenance: %(title)s") % {"title": task.title},
            due=day,
            url=reverse("maintenance:task_edit", args=[task.pk]),
            read_only=True,
        )
        for task, day in calendar_entries(association, start, end)
    ]
