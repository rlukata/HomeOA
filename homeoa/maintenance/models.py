"""Maintenance requests and work orders (MNT-01, MNT-02; simple requests only).

A request comes from an owner or tenant, or is entered by the board on their behalf
. The board turns it into work orders to vendors. A work order's actual cost is the
sum of its linked AP bills (``ap.Bill``); HomeOA never pays vendors and the bills go
through the normal AP approvals. Photos are filed through ``documents.attach``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from homeoa.core.fields import ZERO, MoneyField
from homeoa.core.models import AssociationScopedModel


class MaintenanceRequest(AssociationScopedModel):
    class Status(models.TextChoices):
        NEW = "new", _("New")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")
        DECLINED = "declined", _("Declined")

    class Priority(models.TextChoices):
        LOW = "low", _("Low")
        NORMAL = "normal", _("Normal")
        URGENT = "urgent", _("Urgent")

    unit = models.ForeignKey(
        "registry.Unit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("unit"),
        help_text=_("Leave empty for common areas."),
    )
    location = models.CharField(_("location"), max_length=200, blank=True)
    title = models.CharField(_("summary"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    priority = models.CharField(
        _("priority"), max_length=10, choices=Priority, default=Priority.NORMAL
    )
    status = models.CharField(_("status"), max_length=12, choices=Status, default=Status.NEW)
    submitted_by = models.ForeignKey(
        "registry.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("submitted by"),
    )
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("entered by"),
    )
    created_at = models.DateTimeField(_("submitted at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("maintenance request")
        verbose_name_plural = _("maintenance requests")
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"#{self.pk} {self.title}"

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.NEW, self.Status.IN_PROGRESS)


class MaintenancePhoto(AssociationScopedModel):
    request = models.ForeignKey(
        MaintenanceRequest,
        on_delete=models.PROTECT,
        related_name="photos",
        verbose_name=_("request"),
    )
    version = models.ForeignKey(
        "documents.DocumentVersion",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("file"),
    )

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("maintenance photo")
        verbose_name_plural = _("maintenance photos")
        ordering = ["pk"]

    def __str__(self) -> str:
        return self.version.file_name


class WorkOrder(AssociationScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent")
        SCHEDULED = "scheduled", _("Scheduled")
        IN_PROGRESS = "in_progress", _("In progress")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    OPEN = ("draft", "sent", "scheduled", "in_progress")

    request = models.ForeignKey(
        MaintenanceRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_orders",
        verbose_name=_("request"),
    )
    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.PROTECT,
        related_name="work_orders",
        verbose_name=_("vendor"),
    )
    scope = models.TextField(_("scope of work"))
    status = models.CharField(_("status"), max_length=12, choices=Status, default=Status.DRAFT)
    scheduled_for = models.DateField(_("scheduled for"), null=True, blank=True)
    estimated_cost = MoneyField(_("estimated cost"), null=True, blank=True)
    component = models.ForeignKey(
        "Component",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_orders",
        verbose_name=_("component"),
    )
    preventive_task = models.ForeignKey(
        "PreventiveTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_orders",
        verbose_name=_("preventive task"),
    )
    bills = models.ManyToManyField(
        "ap.Bill",
        through="WorkOrderBill",
        related_name="work_orders",
        blank=True,
        verbose_name=_("bills"),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("work order")
        verbose_name_plural = _("work orders")
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return _("Work order %(n)s") % {"n": self.pk}

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN

    @property
    def actual_cost(self):
        """Sum of the linked bills that are not void (Decimal)."""
        return sum((b.total for b in self.bills.all() if b.status != b.Status.VOID), ZERO)

    def is_overdue(self, today) -> bool:
        return self.is_open and self.scheduled_for is not None and self.scheduled_for < today


class WorkOrderBill(AssociationScopedModel):
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.CASCADE, related_name="+", verbose_name=_("work order")
    )
    bill = models.ForeignKey(
        "ap.Bill", on_delete=models.PROTECT, related_name="+", verbose_name=_("bill")
    )
    linked_at = models.DateTimeField(_("linked at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("work order bill")
        verbose_name_plural = _("work order bills")
        constraints = [
            models.UniqueConstraint(fields=["work_order", "bill"], name="maint_wo_bill_unique")
        ]

    def __str__(self) -> str:
        return f"{self.work_order_id} · {self.bill_id}"


class StatusChange(AssociationScopedModel):
    """Status history shown on request and work order pages (each change is also audited)."""

    request = models.ForeignKey(
        MaintenanceRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="history",
    )
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.CASCADE, null=True, blank=True, related_name="history"
    )
    old_status = models.CharField(_("from"), max_length=12, blank=True)
    new_status = models.CharField(_("to"), max_length=12)
    note = models.CharField(_("note"), max_length=500, blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    changed_at = models.DateTimeField(_("changed at"), auto_now_add=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("status change")
        verbose_name_plural = _("status changes")
        ordering = ["changed_at", "pk"]

    def __str__(self) -> str:
        return f"{self.old_status} → {self.new_status}"

    def _label(self, value):
        model = MaintenanceRequest if self.request_id else WorkOrder
        return dict(model.Status.choices).get(value, value)

    @property
    def old_label(self):
        return self._label(self.old_status) if self.old_status else ""

    @property
    def new_label(self):
        return self._label(self.new_status)


class Component(AssociationScopedModel):
    """A part of the common property the association maintains and one day replaces (MNT-04):
    the inventory a future reserve study (FIN-21) starts from."""

    class Category(models.TextChoices):
        ROOF = "roof", _("Roof")
        SIDING = "siding", _("Siding")
        GUTTERS = "gutters", _("Gutters & downspouts")
        PAINT = "paint", _("Exterior paint")
        PAVING = "paving", _("Paving & parking")
        FENCING = "fencing", _("Fencing")
        DECKS = "decks", _("Decks & stairs")
        PLUMBING = "plumbing", _("Plumbing")
        ELECTRICAL = "electrical", _("Electrical & lighting")
        LANDSCAPING = "landscaping", _("Landscaping")
        OTHER = "other", _("Other")

    class Condition(models.TextChoices):
        GOOD = "good", _("Good")
        FAIR = "fair", _("Fair")
        POOR = "poor", _("Poor")
        UNKNOWN = "unknown", _("Not assessed")

    name = models.CharField(_("name"), max_length=200)
    category = models.CharField(_("category"), max_length=12, choices=Category)
    location = models.CharField(_("location"), max_length=200, blank=True)
    installed_on = models.DateField(_("installed or last replaced"), null=True, blank=True)
    useful_life_years = models.PositiveSmallIntegerField(
        _("useful life (years)"), null=True, blank=True
    )
    condition = models.CharField(
        _("condition"), max_length=8, choices=Condition, default=Condition.UNKNOWN
    )
    replacement_cost = MoneyField(_("replacement cost estimate"), null=True, blank=True)
    cost_as_of = models.DateField(_("estimate date"), null=True, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    active = models.BooleanField(_("in service"), default=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("component")
        verbose_name_plural = _("components")
        ordering = ["category", "name", "pk"]

    def __str__(self) -> str:
        return self.name

    @property
    def replace_year(self) -> int | None:
        if self.installed_on is None or self.useful_life_years is None:
            return None
        return self.installed_on.year + self.useful_life_years

    def remaining_years(self, on) -> int | None:
        year = self.replace_year
        return None if year is None else year - on.year


class ComponentPhoto(AssociationScopedModel):
    """A photo of a component, filed board-only in the document library."""

    component = models.ForeignKey(Component, on_delete=models.CASCADE, related_name="photos")
    document = models.ForeignKey("documents.Document", on_delete=models.PROTECT, related_name="+")

    class Meta(AssociationScopedModel.Meta):
        ordering = ["pk"]


class PreventiveTask(AssociationScopedModel):
    """Recurring upkeep (MNT-03): gutters, dryer vents, roof inspections. ``lead_days``
    before ``next_due`` the daily job opens a draft work order for the task's vendor;
    completing or cancelling it moves ``next_due`` on by ``every_months``."""

    title = models.CharField(_("task"), max_length=200)
    scope = models.TextField(_("scope of work"), blank=True)
    component = models.ForeignKey(
        Component,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        verbose_name=_("component"),
    )
    vendor = models.ForeignKey(
        "vendors.Vendor",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("usual vendor"),
        help_text=_("Without a vendor, HomeOA puts a to-do on Home instead of a work order."),
    )
    every_months = models.PositiveSmallIntegerField(_("repeat every (months)"), default=12)
    next_due = models.DateField(_("next due"))
    lead_days = models.PositiveSmallIntegerField(_("open the work order (days before)"), default=14)
    estimated_cost = MoneyField(_("estimated cost"), null=True, blank=True)
    active = models.BooleanField(_("active"), default=True)
    last_done_on = models.DateField(_("last done"), null=True, blank=True)

    class Meta(AssociationScopedModel.Meta):
        verbose_name = _("preventive task")
        verbose_name_plural = _("preventive tasks")
        ordering = ["next_due", "title", "pk"]

    def __str__(self) -> str:
        return self.title

    def clean(self):
        if self.every_months is not None and self.every_months < 1:
            raise ValidationError({"every_months": _("Repeat at least every month.")})
