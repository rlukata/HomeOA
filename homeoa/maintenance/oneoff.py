"""One-off maintenance jobs and the spending limit.

The board or the Maintenance committee may commission a one-off job (send, schedule or
start a work order) whose estimate is at or under the association's limit without asking
anyone. A job with no estimate, or one above the limit, needs board approval first: the
status change becomes a change request. Raising the estimate of a commissioned job above
the limit is a change request too. The bill for the job still follows the bill approval
rules.

The limit is an association setting (``maintenance_oneoff_limit``); changing it is itself
a change request (Settings > Approval limits). With no limit set, every job needs approval.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.changes import kinds
from homeoa.changes import services as changes

from . import services
from .models import WorkOrder

LIMIT_SETTING = "maintenance_oneoff_limit"
COMMISSION, ESTIMATE = "maintenance.commission", "maintenance.estimate"
COMMISSIONED = (WorkOrder.Status.SENT, WorkOrder.Status.SCHEDULED, WorkOrder.Status.IN_PROGRESS)


def limit(association) -> Decimal:
    raw = (association.settings or {}).get(LIMIT_SETTING)
    try:
        return Decimal(str(raw)) if raw not in (None, "") else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def set_limit(association, value: Decimal, *, reason: str = "") -> None:
    old = (association.settings or {}).get(LIMIT_SETTING)
    settings = dict(association.settings or {})
    settings[LIMIT_SETTING] = str(value)
    association.settings = settings
    association.save(update_fields=["settings"])
    audit.record(
        "association.settings",
        association,
        changes={LIMIT_SETTING: [old, str(value)]},
        message=reason,
    )


def within_limit(estimate, association) -> bool:
    return estimate is not None and estimate <= limit(association)


def commissions(wo: WorkOrder, new_status: str) -> bool:
    return wo.status == WorkOrder.Status.DRAFT and new_status in COMMISSIONED


def needs_approval(wo: WorkOrder, new_status: str) -> bool:
    return commissions(wo, new_status) and not within_limit(wo.estimated_cost, wo.association)


def estimate_needs_approval(wo: WorkOrder, new_estimate) -> bool:
    """Raising a commissioned job's estimate above the limit needs approval."""
    if wo.status not in COMMISSIONED or new_estimate is None:
        return False
    if wo.estimated_cost is not None and new_estimate <= wo.estimated_cost:
        return False
    return not within_limit(new_estimate, wo.association)


def pending_for(wo: WorkOrder):
    return changes.pending(wo.association).filter(
        kind__in=(COMMISSION, ESTIMATE), payload__work_order=wo.pk
    )


def _money(value) -> str:
    return f"${value:,.2f}" if value is not None else _("no estimate")


def propose_commission(wo: WorkOrder, *, status, note, scheduled_for, user):
    status_label = str(dict(WorkOrder.Status.choices)[status])
    details = [
        (_("Work order"), f"#{wo.pk} · {wo.vendor}"),
        (_("Scope"), wo.scope),
        (_("Estimate"), _money(wo.estimated_cost)),
        (_("One-off limit"), _money(limit(wo.association))),
        (_("New status"), status_label),
    ]
    if note:
        details.append((_("Note"), note))
    return changes.propose(
        wo.association,
        COMMISSION,
        {
            "work_order": wo.pk,
            "status": status,
            "note": note,
            "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
        },
        user=user,
        summary=_("Approve work order #%(n)s with %(vendor)s (%(cost)s)")
        % {"n": wo.pk, "vendor": wo.vendor, "cost": _money(wo.estimated_cost)},
        details=details,
    )


def propose_estimate(wo: WorkOrder, form, *, user):
    new = form.cleaned_data["estimated_cost"]
    details = [
        (_("Work order"), f"#{wo.pk} · {wo.vendor}"),
        (_("Estimate"), f"{_money(wo.estimated_cost)} → {_money(new)}"),
        (_("One-off limit"), _money(limit(wo.association))),
    ]
    return changes.propose(
        wo.association,
        ESTIMATE,
        {
            "work_order": wo.pk,
            "vendor": form.cleaned_data["vendor"].pk,
            "scope": form.cleaned_data["scope"],
            "estimated_cost": str(new),
            "scheduled_for": (
                form.cleaned_data["scheduled_for"].isoformat()
                if form.cleaned_data["scheduled_for"]
                else None
            ),
        },
        user=user,
        summary=_("Raise work order #%(n)s to %(cost)s") % {"n": wo.pk, "cost": _money(new)},
        details=details,
    )


def _work_order(change) -> WorkOrder:
    return WorkOrder.all_associations.get(
        association=change.association, pk=change.payload["work_order"]
    )


def _apply_commission(change) -> str:
    import datetime as dt

    wo = _work_order(change)
    p = change.payload
    if wo.status != WorkOrder.Status.DRAFT:
        raise changes.ChangeError(_("The work order is no longer a draft."))
    services.set_work_order_status(
        wo,
        p["status"],
        user=change.proposed_by,
        note=p.get("note") or "",
        scheduled_for=dt.date.fromisoformat(p["scheduled_for"]) if p.get("scheduled_for") else None,
    )
    return reverse("maintenance:work_order", args=[wo.pk])


def _apply_estimate(change) -> str:
    import datetime as dt

    from homeoa.vendors.models import Vendor

    wo = _work_order(change)
    p = change.payload
    wo.vendor = Vendor.all_associations.get(association=change.association, pk=p["vendor"])
    wo.scope = p["scope"]
    wo.estimated_cost = Decimal(p["estimated_cost"])
    wo.scheduled_for = dt.date.fromisoformat(p["scheduled_for"]) if p.get("scheduled_for") else None
    wo.save()
    return reverse("maintenance:work_order", args=[wo.pk])


def register() -> None:
    kinds.register(
        kinds.Kind(COMMISSION, gettext_lazy("Work order over the limit"), _apply_commission)
    )
    kinds.register(
        kinds.Kind(ESTIMATE, gettext_lazy("Higher work order estimate"), _apply_estimate)
    )


def apply_setup_answer(association, answers: dict, *, resolution, on, reason: str) -> None:
    """The setup wizard's answer for the one-off limit, once its resolution is adopted."""
    set_limit(association, Decimal(answers["maintenance.oneoff_limit"]), reason=reason)
