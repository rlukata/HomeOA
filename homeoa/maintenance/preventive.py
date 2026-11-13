"""Preventive task dates, shared by work orders and preventive maintenance.

Completing or cancelling a preventive work order moves its task to the next date, and Home
lists due tasks that have no vendor yet.
"""

from __future__ import annotations

import calendar
import datetime as dt

from django.urls import reverse
from django.utils.translation import gettext as _

from .models import PreventiveTask, WorkOrder


def add_months(day: dt.date, months: int) -> dt.date:
    y, m = divmod(day.month - 1 + months, 12)
    year, month = day.year + y, m + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def opens_on(task: PreventiveTask) -> dt.date:
    return task.next_due - dt.timedelta(days=task.lead_days)


def after_status_change(wo: WorkOrder, *, on: dt.date) -> None:
    """A preventive work order completed or cancelled: schedule the task's next date."""
    task = wo.preventive_task
    if task is None or wo.status not in (WorkOrder.Status.COMPLETED, WorkOrder.Status.CANCELLED):
        return
    if wo.status == WorkOrder.Status.COMPLETED:
        task.last_done_on = on
    task.next_due = add_months(task.next_due, task.every_months)
    task.save(update_fields=["last_done_on", "next_due"])


def todo_items(association) -> list[dict]:
    """Tasks with no vendor that are within their lead time."""
    from homeoa.access import permissions as perms
    from homeoa.core.context import association_today

    today = association_today(association)
    out = []
    for task in PreventiveTask.all_associations.filter(
        association=association, active=True, vendor__isnull=True
    ):
        if opens_on(task) > today:
            continue
        out.append(
            {
                "id": f"maint-preventive-{task.pk}-{task.next_due.isoformat()}",
                "kind": "requests",
                "title": _("Preventive maintenance due: %(t)s") % {"t": task.title},
                "sub": _("Due %(date)s · no vendor set") % {"date": task.next_due.isoformat()},
                "due": task.next_due,
                "url": reverse("maintenance:task_edit", args=[task.pk]),
                "action": _("Open"),
                "perm": perms.MAINTENANCE_MANAGE,
            }
        )
    return out
