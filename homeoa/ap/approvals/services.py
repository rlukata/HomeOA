"""Approve or reject a submitted bill (FIN-12).

* ``approve(bill, user)``: records one approval per person per submission. When the count
  reaches the matching rule's requirement the bill becomes approved.
* ``reject(bill, user, reason)``: returns the bill to draft. Submit already posted the
  accrual, so reject posts its reversal through ``ledger.reverse_entry`` (dated
  today) and clears ``bill.entry``; resubmitting posts a fresh accrual and starts a new
  round of approvals.

Errors are :class:`ApprovalError` (a ``BillError``) with a translated message; a user who
may not decide on the bill gets ``PermissionDenied``. Everything is audited.
"""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.core.context import association_today
from homeoa.ledger import services as ledger

from ..models import Bill
from ..services import BillError, _lock
from . import notifications, rules
from .models import Approval


class ApprovalError(BillError):
    pass


def approvals_for(bill: Bill):
    """Approvals counted for the bill's current submission."""
    if not bill.entry_id:
        return Approval.all_associations.none()
    return Approval.all_associations.filter(
        bill=bill, entry_id=bill.entry_id, decision=Approval.Decision.APPROVED
    )


def progress(bill: Bill) -> tuple[int, int]:
    """``(approvals so far, approvals required)`` for the current submission."""
    return approvals_for(bill).count(), rules.requirement_for(bill).required


def can_approve(bill: Bill, user) -> bool:
    if bill.status != Bill.Status.PENDING:
        return False
    req = rules.requirement_for(bill)
    if not rules.is_eligible(user, bill, req):
        return False
    if rules.is_submitter(user, bill) and not req.allow_submitter:
        return False
    return not approvals_for(bill).filter(user=user).exists()


def approve(bill: Bill, user) -> Bill:
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status != Bill.Status.PENDING:
            raise ApprovalError(_("Only a bill waiting for approval can be approved."))
        req = rules.requirement_for(bill)
        if not rules.is_eligible(user, bill, req):
            raise PermissionDenied
        if rules.is_submitter(user, bill) and not req.allow_submitter:
            raise ApprovalError(_("The person who entered a bill cannot also approve it."))
        if approvals_for(bill).filter(user=user).exists():
            raise ApprovalError(_("You have already approved this bill."))
        Approval.objects.create(
            association=bill.association,
            bill=bill,
            entry_id=bill.entry_id,
            user=user,
            decision=Approval.Decision.APPROVED,
            rule=req.rule,
        )
        count = approvals_for(bill).count()
        audit.record("ap.bill_approval", bill, actor=user, message=f"{count} of {req.required}")
        if count >= req.required:
            bill.status = Bill.Status.APPROVED
            bill.approved_at = timezone.now()
            bill.save()
            audit.record("ap.bill_approved", bill, actor=user, message=f"{bill.total:.2f}")
            notifications.notify_submitter(
                bill, approved=True, entry_id=bill.entry_id, decider=user
            )
    return bill


def reject(bill: Bill, user, reason: str) -> Bill:
    reason = (reason or "").strip()
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status != Bill.Status.PENDING:
            raise ApprovalError(_("Only a bill waiting for approval can be rejected."))
        req = rules.requirement_for(bill)
        if not rules.is_eligible(user, bill, req):
            raise PermissionDenied
        if not reason:
            raise ApprovalError(_("Give a reason for rejecting the bill."))
        Approval.objects.create(
            association=bill.association,
            bill=bill,
            entry_id=bill.entry_id,
            user=user,
            decision=Approval.Decision.REJECTED,
            rule=req.rule,
            reason=reason[:255],
        )
        try:
            ledger.reverse_entry(
                bill.entry,
                date=association_today(bill.association),
                memo=_("Rejection of bill %(bill)s") % {"bill": bill},
                posted_by=user,
            )
        except ledger.LedgerError as exc:
            raise ApprovalError(str(exc)) from exc
        entry_id = bill.entry_id
        bill.entry = None
        bill.submitted_at = None
        bill.approved_at = None
        bill.status = Bill.Status.DRAFT
        bill.save()
        audit.record("ap.bill_rejected", bill, actor=user, message=reason[:200])
        notifications.notify_submitter(
            bill, approved=False, entry_id=entry_id, decider=user, reason=reason
        )
    return bill


def waiting_on(bill: Bill) -> list:
    """Users who may still approve the bill's current submission (for to-dos and the page)."""
    if bill.status != Bill.Status.PENDING:
        return []
    from homeoa.access.models import RoleAssignment
    from homeoa.access.services import today_for

    day = today_for(bill.association)
    assignments = (
        RoleAssignment.all_associations.filter(association=bill.association_id, start__lte=day)
        .exclude(end__lt=day)
        .select_related("user")
        .order_by("user__full_name", "user_id")
    )
    seen, out = set(), []
    for a in assignments:
        if a.user_id in seen:
            continue
        seen.add(a.user_id)
        if a.user.is_active and can_approve(bill, a.user):
            out.append(a.user)
    return out
