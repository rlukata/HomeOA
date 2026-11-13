"""Maintenance request and work order services (MNT-01, MNT-02).

Every status change writes a ``StatusChange`` row (history shown on the page) and an
explicit audit event; the models are also registered with ``audit`` for field diffs.
Linking a bill never touches the bill itself: approvals and posting stay in ``ap``.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.ap.models import Bill
from homeoa.core.context import association_today
from homeoa.documents import services as documents
from homeoa.documents.models import Category, Visibility

from .models import MaintenancePhoto, MaintenanceRequest, StatusChange, WorkOrder, WorkOrderBill

MAX_PHOTOS = 10


class MaintenanceError(ValidationError):
    pass


def check_photo(upload) -> None:
    """Images only; size and type limits from the documents storage settings."""
    ctype = documents.check_upload(upload.name, upload.size)
    if not ctype.startswith("image/"):
        raise MaintenanceError(_("%(name)s is not a photo (PNG or JPEG).") % {"name": upload.name})


def _actor(user):
    return user if getattr(user, "is_authenticated", False) else None


def add_photos(req: MaintenanceRequest, uploads, *, user=None) -> list[MaintenancePhoto]:
    uploads = list(uploads)
    if req.photos.count() + len(uploads) > MAX_PHOTOS:
        raise MaintenanceError(_("A request can have at most %(n)s photos.") % {"n": MAX_PHOTOS})
    for upload in uploads:
        check_photo(upload)
    photos = []
    for upload in uploads:
        upload.seek(0)
        version = documents.attach(
            req.association,
            filename=upload.name,
            content=upload.read(),
            title=_("Maintenance request #%(n)s photo") % {"n": req.pk},
            category=Category.OTHER,
            visibility=Visibility.BOARD,
            user=_actor(user),
        )
        photos.append(MaintenancePhoto.objects.create(request=req, version=version))
    return photos


@transaction.atomic
def submit_request(
    *,
    title: str,
    unit=None,
    location: str = "",
    description: str = "",
    priority: str = MaintenanceRequest.Priority.NORMAL,
    submitted_by=None,
    entered_by=None,
    photos=(),
) -> MaintenanceRequest:
    req = MaintenanceRequest.objects.create(
        title=title,
        unit=unit,
        location=location,
        description=description,
        priority=priority,
        submitted_by=submitted_by,
        entered_by=_actor(entered_by),
    )
    StatusChange.objects.create(request=req, new_status=req.status, changed_by=_actor(entered_by))
    if photos:
        add_photos(req, photos, user=entered_by)
    return req


def _change(obj, field_name, status, *, user, note="", **extra) -> bool:
    old = obj.status
    if old == status and not extra:
        return False
    obj.status = status
    for key, value in extra.items():
        setattr(obj, key, value)
    obj.save()
    if old != status:
        StatusChange.objects.create(
            **{field_name: obj},
            old_status=old,
            new_status=status,
            note=note[:500],
            changed_by=_actor(user),
        )
        audit.record(
            f"maintenance.{field_name}_status",
            obj,
            actor=_actor(user),
            changes={"status": [old, status]},
            message=note[:500],
        )
    return True


@transaction.atomic
def set_request_status(req: MaintenanceRequest, status: str, *, user=None, note="") -> bool:
    if status not in MaintenanceRequest.Status.values:
        raise MaintenanceError(_("Unknown status."))
    return _change(req, "request", status, user=user, note=note)


@transaction.atomic
def create_work_order(
    *,
    vendor,
    scope: str,
    request: MaintenanceRequest | None = None,
    estimated_cost=None,
    scheduled_for=None,
    user=None,
) -> WorkOrder:
    if request is not None and request.association_id != vendor.association_id:
        raise MaintenanceError(_("The vendor belongs to another association."))
    wo = WorkOrder.objects.create(
        request=request,
        vendor=vendor,
        scope=scope,
        estimated_cost=estimated_cost,
        scheduled_for=scheduled_for,
        created_by=_actor(user),
    )
    StatusChange.objects.create(work_order=wo, new_status=wo.status, changed_by=_actor(user))
    if request is not None and request.status == MaintenanceRequest.Status.NEW:
        set_request_status(
            request,
            MaintenanceRequest.Status.IN_PROGRESS,
            user=user,
            note=_("Work order %(n)s created") % {"n": wo.pk},
        )
    return wo


@transaction.atomic
def set_work_order_status(
    wo: WorkOrder, status: str, *, user=None, note="", scheduled_for=None
) -> bool:
    if status not in WorkOrder.Status.values:
        raise MaintenanceError(_("Unknown status."))
    extra = {}
    if scheduled_for is not None and scheduled_for != wo.scheduled_for:
        extra["scheduled_for"] = scheduled_for
    changed = _change(wo, "work_order", status, user=user, note=note, **extra)
    if changed and wo.preventive_task_id:
        from . import preventive

        preventive.after_status_change(wo, on=association_today(wo.association))
    return changed


def linkable_bills(wo: WorkOrder):
    """The vendor's bills in this association that are not void and not yet linked."""
    linked = WorkOrderBill.objects.filter(work_order=wo).values("bill_id")
    return (
        Bill.objects.filter(vendor_id=wo.vendor_id, association_id=wo.association_id)
        .exclude(status=Bill.Status.VOID)
        .exclude(pk__in=linked)
        .order_by("-bill_date", "-pk")
    )


@transaction.atomic
def link_bill(wo: WorkOrder, bill: Bill, *, user=None) -> WorkOrderBill:
    if bill.association_id != wo.association_id or bill.vendor_id != wo.vendor_id:
        raise MaintenanceError(_("Only this work order's vendor's bills can be linked."))
    if bill.status == Bill.Status.VOID:
        raise MaintenanceError(_("A void bill cannot be linked."))
    link, created = WorkOrderBill.objects.get_or_create(work_order=wo, bill=bill)
    if created:
        audit.record(
            "maintenance.bill_linked",
            wo,
            actor=_actor(user),
            changes={"bill": [None, bill.pk]},
        )
    return link


@transaction.atomic
def unlink_bill(wo: WorkOrder, bill: Bill, *, user=None) -> bool:
    deleted, _rows = WorkOrderBill.objects.filter(work_order=wo, bill=bill).delete()
    if deleted:
        audit.record(
            "maintenance.bill_unlinked",
            wo,
            actor=_actor(user),
            changes={"bill": [bill.pk, None]},
        )
    return bool(deleted)


def todo_items(association) -> list[dict]:
    """Home to-dos: new requests to triage; open work orders past their scheduled date;
    preventive tasks with no vendor that are due soon."""
    from . import preventive

    return (
        preventive.todo_items(association)
        + request_todo_items(association)
        + late_work_order_todo_items(association)
    )


def request_todo_items(association) -> list[dict]:
    """Home to-dos: new maintenance requests waiting to be triaged."""
    items = []
    new = MaintenanceRequest.all_associations.filter(
        association=association, status=MaintenanceRequest.Status.NEW
    ).select_related("unit")
    for req in new.order_by("created_at", "pk"):
        where = req.unit.number if req.unit_id else (req.location or _("Common area"))
        items.append(
            {
                "id": f"maint-triage-{req.pk}",
                "kind": "requests",
                "title": _("Triage maintenance request: %(t)s") % {"t": req.title},
                "sub": _("%(where)s · %(priority)s priority")
                % {"where": where, "priority": req.get_priority_display()},
                "url": reverse("maintenance:detail", args=[req.pk]),
                "action": _("Review"),
            }
        )
    return items


def work_order_todo_items(association) -> list[dict]:
    """Home to-dos: preventive tasks with no vendor that are due soon, then open work orders
    past their scheduled date."""
    from . import preventive

    return preventive.todo_items(association) + late_work_order_todo_items(association)


def late_work_order_todo_items(association) -> list[dict]:
    """Home to-dos: open work orders past their scheduled date."""
    items = []
    today = association_today(association)
    late = WorkOrder.all_associations.filter(
        association=association,
        status__in=WorkOrder.OPEN,
        scheduled_for__lt=today,
    ).select_related("vendor")
    for wo in late.order_by("scheduled_for", "pk"):
        items.append(
            {
                "id": f"maint-wo-late-{wo.pk}",
                "kind": "requests",
                "title": _("Work order %(n)s is past its scheduled date") % {"n": wo.pk},
                "sub": _("%(vendor)s · scheduled %(date)s")
                % {"vendor": wo.vendor.name, "date": wo.scheduled_for.isoformat()},
                "due": wo.scheduled_for,
                "url": reverse("maintenance:work_order", args=[wo.pk]),
                "action": _("Update"),
            }
        )
    return items
