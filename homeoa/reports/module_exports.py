"""CSV exports of each optional feature's records, for Money > Reports > Exports.

They read the apps' models directly and work whether the feature is on or off, so the
board can still answer a records request after switching a feature off. Each export has
the permission that opens the feature's own board pages.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.db.models import Count
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.arc.models import ArcRequest
from homeoa.community.models import Announcement, Poll, PollAnswer
from homeoa.enforcement.models import Violation
from homeoa.maintenance.models import MaintenanceRequest, WorkOrder
from homeoa.payments.models import AutopayCharge, AutopayEnrollment

from .export import DATE, MONEY, Column, TableSpec


def _date(value):
    return value.date() if hasattr(value, "date") else value


def _unit(obj):
    return obj.unit.number if obj.unit_id else ""


def violations(association) -> TableSpec:
    rows = []
    qs = (
        Violation.all_associations.filter(association=association)
        .select_related("unit", "category")
        .prefetch_related("steps", "hearings")
        .order_by("observed_on", "pk")
    )
    for v in qs:
        steps = "; ".join(
            f"{s.get_kind_display()} {s.sent_on.isoformat()}"
            for s in sorted(v.steps.all(), key=lambda s: (s.sent_on, s.pk))
        )
        hearings = sorted(v.hearings.all(), key=lambda h: (h.hearing_on, h.pk))
        fines = sum((h.fine_amount for h in hearings if h.fine_amount is not None), None)
        rows.append(
            [
                v.pk,
                _unit(v),
                str(v.category),
                v.observed_on,
                v.description,
                v.get_status_display(),
                v.cured_on,
                steps,
                "; ".join(h.hearing_on.isoformat() for h in hearings),
                "; ".join(h.get_outcome_display() for h in hearings if h.outcome),
                fines,
            ]
        )
    columns = [
        Column(_("Number")),
        Column(_("Unit")),
        Column(_("Category")),
        Column(_("Observed on"), DATE),
        Column(_("Description")),
        Column(_("Status")),
        Column(_("Cured on"), DATE),
        Column(_("Notices sent")),
        Column(_("Hearing dates")),
        Column(_("Hearing decisions")),
        Column(_("Fines"), MONEY),
    ]
    return TableSpec(_("Violations"), columns, rows, filename="violations", pii=True)


def design_requests(association) -> TableSpec:
    qs = (
        ArcRequest.all_associations.filter(association=association)
        .select_related("unit")
        .order_by("submitted_on", "pk")
    )
    rows = [
        [
            r.pk,
            _unit(r),
            r.title,
            r.submitted_on,
            r.get_status_display(),
            r.decide_by,
            r.decided_on,
            r.conditions,
            r.decision_note,
            _("Yes") if r.deemed else _("No"),
        ]
        for r in qs
    ]
    columns = [
        Column(_("Number")),
        Column(_("Unit")),
        Column(_("Title")),
        Column(_("Submitted on"), DATE),
        Column(_("Status")),
        Column(_("Decide by"), DATE),
        Column(_("Decided on"), DATE),
        Column(_("Conditions")),
        Column(_("Decision note")),
        Column(_("Deemed approved by law")),
    ]
    return TableSpec(
        _("Design approval requests"), columns, rows, filename="design-requests", pii=True
    )


def maintenance_requests(association) -> TableSpec:
    qs = (
        MaintenanceRequest.all_associations.filter(association=association)
        .select_related("unit")
        .order_by("created_at", "pk")
    )
    rows = [
        [
            r.pk,
            _date(r.created_at),
            _unit(r),
            r.location,
            r.title,
            r.description,
            r.get_priority_display(),
            r.get_status_display(),
        ]
        for r in qs
    ]
    columns = [
        Column(_("Number")),
        Column(_("Submitted on"), DATE),
        Column(_("Unit")),
        Column(_("Location")),
        Column(_("Summary")),
        Column(_("Description")),
        Column(_("Priority")),
        Column(_("Status")),
    ]
    return TableSpec(
        _("Maintenance requests"), columns, rows, filename="maintenance-requests", pii=True
    )


def work_orders(association) -> TableSpec:
    qs = (
        WorkOrder.all_associations.filter(association=association)
        .select_related("vendor")
        .order_by("created_at", "pk")
    )
    rows = [
        [
            w.pk,
            _date(w.created_at),
            str(w.vendor) if w.vendor_id else "",
            w.scope,
            w.get_status_display(),
            w.scheduled_for,
            w.request_id or "",
        ]
        for w in qs
    ]
    columns = [
        Column(_("Number")),
        Column(_("Created on"), DATE),
        Column(_("Vendor")),
        Column(_("Scope of work")),
        Column(_("Status")),
        Column(_("Scheduled for"), DATE),
        Column(_("Maintenance request number")),
    ]
    return TableSpec(_("Work orders"), columns, rows, filename="work-orders")


def announcements(association) -> TableSpec:
    qs = Announcement.all_associations.filter(association=association).order_by("publish_on", "pk")
    rows = [
        [
            a.pk,
            a.title,
            a.body,
            a.get_audience_display(),
            a.publish_on,
            a.expires_on,
            _("Yes") if a.also_notify else _("No"),
            a.notified_on,
        ]
        for a in qs
    ]
    columns = [
        Column(_("Number")),
        Column(_("Title")),
        Column(_("Message")),
        Column(_("Audience")),
        Column(_("Publish on"), DATE),
        Column(_("Expires on"), DATE),
        Column(_("Also emailed")),
        Column(_("Emailed on"), DATE),
    ]
    return TableSpec(_("Announcements"), columns, rows, filename="announcements")


def polls(association) -> TableSpec:
    counts: dict[int, dict[int, int]] = {}
    answers = (
        PollAnswer.all_associations.filter(association=association)
        .values("poll_id", "choice")
        .annotate(n=Count("pk"))
    )
    for row in answers:
        counts.setdefault(row["poll_id"], {})[row["choice"]] = row["n"]
    rows = []
    for p in Poll.all_associations.filter(association=association).order_by("opens_on", "pk"):
        tally = counts.get(p.pk, {})
        results = "; ".join(f"{c}: {tally.get(i, 0)}" for i, c in enumerate(p.choices))
        rows.append([p.pk, p.question, p.opens_on, p.closes_on, p.closed_early_on, results])
    columns = [
        Column(_("Number")),
        Column(_("Question")),
        Column(_("Opens on"), DATE),
        Column(_("Closes on"), DATE),
        Column(_("Closed early on"), DATE),
        Column(_("Results")),
    ]
    return TableSpec(_("Polls"), columns, rows, filename="polls")


def autopay(association) -> TableSpec:
    """Enrollments and the debits under them, one row each."""
    rows = []
    enrollments = (
        AutopayEnrollment.all_associations.filter(association=association)
        .select_related("unit")
        .order_by("starts_on", "pk")
    )
    for e in enrollments:
        rows.append(
            [
                _("Enrollment"),
                e.pk,
                _unit(e),
                e.starts_on,
                _date(e.cancelled_at),
                e.get_rule_display(),
                e.fixed_amount,
                None,
                "",
                None,
                None,
            ]
        )
    charges = (
        AutopayCharge.all_associations.filter(association=association)
        .select_related("unit")
        .order_by("period", "pk")
    )
    for c in charges:
        rows.append(
            [
                _("Debit"),
                c.enrollment_id,
                _unit(c),
                None,
                None,
                "",
                None,
                c.period,
                c.get_status_display(),
                c.debit_on,
                c.amount,
            ]
        )
    columns = [
        Column(_("Record")),
        Column(_("Enrollment number")),
        Column(_("Unit")),
        Column(_("Starts on"), DATE),
        Column(_("Turned off on"), DATE),
        Column(_("Amount rule")),
        Column(_("Fixed amount"), MONEY),
        Column(_("Month"), DATE),
        Column(_("Debit status")),
        Column(_("Debit date"), DATE),
        Column(_("Debit amount"), MONEY),
    ]
    return TableSpec(_("Autopay history"), columns, rows, filename="autopay-history", pii=True)


@dataclass(frozen=True)
class ModuleExport:
    slug: str
    label: str
    build: Callable[..., TableSpec]
    perms: tuple[str, ...]


EXPORTS = {
    e.slug: e
    for e in (
        ModuleExport(
            "violations", gettext_lazy("Violations"), violations, (perms.VIOLATIONS_MANAGE,)
        ),
        ModuleExport(
            "design-requests",
            gettext_lazy("Design approval requests"),
            design_requests,
            (perms.ARCH_REVIEW, perms.ARCH_DECIDE),
        ),
        ModuleExport(
            "maintenance-requests",
            gettext_lazy("Maintenance requests"),
            maintenance_requests,
            (perms.MAINTENANCE_MANAGE,),
        ),
        ModuleExport(
            "work-orders",
            gettext_lazy("Work orders"),
            work_orders,
            (perms.MAINTENANCE_MANAGE,),
        ),
        ModuleExport(
            "announcements",
            gettext_lazy("Announcements"),
            announcements,
            (perms.ANNOUNCEMENTS_MANAGE,),
        ),
        ModuleExport("polls", gettext_lazy("Polls"), polls, (perms.ANNOUNCEMENTS_MANAGE,)),
        ModuleExport("autopay", gettext_lazy("Autopay history"), autopay, (perms.FINANCE_READ,)),
    )
}
