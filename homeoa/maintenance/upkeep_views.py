"""Requests > Preventive and Requests > Components (Maintenance committee and board)."""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.core.context import association_today
from homeoa.documents import services as documents
from homeoa.ui.nav import get_hub

from . import upkeep
from .forms import ComponentForm, PreventiveTaskForm
from .models import Component, ComponentPhoto, PreventiveTask

MANAGE = perms.MAINTENANCE_MANAGE


def _render(request, template, context, tab, status=200):
    hub = get_hub("requests", request.association)
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == tab}
        for i, t in enumerate(hub.subtabs)
    ]
    ctx = {
        "current_hub": hub,
        "subtabs": tabs,
        "today": association_today(request.association),
        **context,
    }
    return render(request, template, ctx, status=status)


@require_perm(MANAGE)
def tasks(request):
    rows = PreventiveTask.objects.select_related("vendor", "component")
    return _render(request, "maintenance/tasks.html", {"tasks": rows}, "preventive")


@require_perm(MANAGE)
def task_edit(request, pk=None):
    task = get_object_or_404(PreventiveTask.objects, pk=pk) if pk else None
    if request.method == "POST" and request.POST.get("action") == "done" and task:
        upkeep.mark_done(task, on=association_today(request.association))
        messages.success(request, _("Marked done. Next due %(d)s.") % {"d": task.next_due})
        return redirect("maintenance:tasks")
    form = PreventiveTaskForm(request.POST or None, instance=task)
    if request.method == "POST" and form.is_valid():
        form.instance.association = request.association
        upkeep.save_task(form.instance)
        messages.success(request, _("Task saved."))
        return redirect("maintenance:tasks")
    return _render(
        request,
        "maintenance/task_form.html",
        {
            "form": form,
            "task": task,
            "work_orders": task.work_orders.select_related("vendor") if task else [],
        },
        "preventive",
        status=400 if request.method == "POST" else 200,
    )


@require_perm(MANAGE)
def components(request):
    today = association_today(request.association)
    rows = list(Component.objects.all())
    for c in rows:
        c.remaining = c.remaining_years(today)
    total = sum((c.replacement_cost for c in rows if c.active and c.replacement_cost), 0)
    return _render(
        request, "maintenance/components.html", {"components": rows, "total": total}, "components"
    )


@require_perm(MANAGE)
def component_edit(request, pk=None):
    component = get_object_or_404(Component.objects, pk=pk) if pk else None
    form = ComponentForm(request.POST or None, instance=component)
    if request.method == "POST" and form.is_valid():
        form.instance.association = request.association
        saved = upkeep.save_component(form.instance)
        messages.success(request, _("Component saved."))
        return redirect("maintenance:component_edit", pk=saved.pk)
    context = {"form": form, "component": component}
    if component:
        context.update(
            photos=component.photos.select_related("document"),
            tasks=component.tasks.all(),
            work_orders=component.work_orders.select_related("vendor"),
        )
    return _render(
        request,
        "maintenance/component_form.html",
        context,
        "components",
        status=400 if request.method == "POST" else 200,
    )


@require_POST
@require_perm(MANAGE)
def component_photos(request, pk):
    component = get_object_or_404(Component.objects, pk=pk)
    try:
        for f in request.FILES.getlist("photos"):
            upkeep.add_photo(component, filename=f.name, content=f.read(), user=request.user)
        messages.success(request, _("Photos added."))
    except ValidationError as exc:
        for m in exc.messages:
            messages.error(request, m)
    return redirect("maintenance:component_edit", pk=component.pk)


@require_perm(MANAGE)
def component_photo(request, pk, photo_pk):
    photo = get_object_or_404(
        ComponentPhoto.objects.select_related("document__current_version"),
        pk=photo_pk,
        component_id=pk,
    )
    version = photo.document.current_version
    response = HttpResponse(documents.read_file(version), content_type=version.content_type)
    response["Content-Disposition"] = f'inline; filename="{version.file_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
