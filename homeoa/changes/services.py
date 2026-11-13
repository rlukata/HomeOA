"""Propose, approve, reject, withdraw and apply change requests.

* ``propose(association, kind, payload, *, user, summary, details=(), reason="",
  excluded=())``: ``excluded`` are users who may not approve it (e.g. the person whose
  role it changes).
* ``approve(change, user, comment="")`` / ``reject(change, user, comment)`` /
  ``withdraw(change, user)``
* ``can_decide(change, user)``: an active board member who is not the proposer and has
  not decided yet.
* ``waiting_on(change)``: the board members who could still approve it.

A kind approved ``by_resolution`` (dues, special assessments) cites a board resolution: it
applies as soon as that resolution is adopted (at once if it already is), and no clicks are
needed. Each resolution backs one change.

Who must approve: one other board member when the proposer is on the board, two when not.
A board too small for that needs every other board member instead; with nobody else on the
board (a new association being set up) the change applies at once, since nobody could ever
approve it.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.access import services as access

from . import kinds
from .models import ChangeDecision, ChangeRequest

logger = logging.getLogger(__name__)


class ChangeError(ValueError):
    pass


def _required(on_board: bool) -> int:
    return 1 if on_board else 2


def board_members(association) -> list:
    return access.board_members(association)


@transaction.atomic
def propose(
    association,
    kind: str,
    payload: dict,
    *,
    user,
    summary: str,
    details=(),
    reason: str = "",
    excluded=(),
    resolution=None,
) -> ChangeRequest:
    kind_obj = kinds.get(kind)
    if kind_obj is None:
        raise ChangeError(f"Unknown change kind {kind!r}")
    if kind_obj.by_resolution:
        return _propose_by_resolution(
            association,
            kind,
            payload,
            user=user,
            summary=summary,
            details=details,
            reason=reason,
            resolution=resolution,
        )
    on_board = access.is_board_member(user, association)
    excluded_ids = sorted({u.pk for u in excluded if u is not None and u.pk})
    others = [u for u in board_members(association) if u.pk != user.pk and u.pk not in excluded_ids]
    change = ChangeRequest.all_associations.create(
        association=association,
        kind=kind,
        summary=str(summary)[:300],
        details=[[str(label), str(value)] for label, value in details],
        payload=payload,
        reason=reason,
        proposed_by=user,
        proposer_on_board=on_board,
        required_approvals=min(_required(on_board), len(others)),
        excluded=excluded_ids,
    )
    if not others:
        _apply(change)
        return change
    from . import notifications

    notifications.notify_approvers(change, waiting_on(change))
    return change


def usable_resolutions(association):
    """Draft or adopted resolutions not yet backing a change."""
    from homeoa.governance.models import Resolution

    return Resolution.all_associations.filter(
        association=association,
        status__in=(Resolution.Status.DRAFT, Resolution.Status.ADOPTED),
        change_request__isnull=True,
    ).order_by("-year", "-seq")


def _propose_by_resolution(
    association, kind, payload, *, user, summary, details, reason, resolution
):
    from homeoa.governance.models import Resolution

    if resolution is None:
        raise ChangeError(_("Choose the board resolution that approves this change."))
    if not usable_resolutions(association).filter(pk=resolution.pk).exists():
        raise ChangeError(_("That resolution is rescinded or already backs another change."))
    change = ChangeRequest.all_associations.create(
        association=association,
        kind=kind,
        summary=str(summary)[:300],
        details=[[str(label), str(value)] for label, value in details],
        payload=payload,
        reason=reason,
        proposed_by=user,
        proposer_on_board=access.is_board_member(user, association),
        required_approvals=0,
        resolution=resolution,
    )
    if resolution.status == Resolution.Status.ADOPTED:
        _apply(change)
    return change


def resolution_adopted(resolution) -> None:
    """Called when a resolution is adopted: apply the change it backs, if any."""
    change = (
        ChangeRequest.all_associations.select_for_update()
        .filter(resolution=resolution, status=ChangeRequest.Status.PENDING)
        .first()
    )
    if change is not None:
        _apply(change)
        from . import notifications

        notifications.notify_proposer(change)


def decided_by(change: ChangeRequest) -> set[int]:
    return set(change.decisions.values_list("user_id", flat=True))


def waiting_on(change: ChangeRequest) -> list:
    if not change.is_pending or change.resolution_id:
        return []
    done = decided_by(change) | {change.proposed_by_id} | set(change.excluded or ())
    return [u for u in board_members(change.association) if u.pk not in done]


def can_decide(change: ChangeRequest, user) -> bool:
    if not change.is_pending or user is None or not getattr(user, "pk", None):
        return False
    if change.resolution_id:
        return False  # approved by adopting the resolution, not by clicks
    if user.pk == change.proposed_by_id or user.pk in decided_by(change):
        return False
    if user.pk in (change.excluded or ()):
        return False
    return access.is_board_member(user, change.association)


def _lock(change: ChangeRequest) -> ChangeRequest:
    return ChangeRequest.all_associations.select_for_update().get(pk=change.pk)


def _close(change: ChangeRequest, status: str, **fields) -> None:
    change.status = status
    change.closed_at = timezone.now()
    for name, value in fields.items():
        setattr(change, name, value)
    change.save()


def _apply(change: ChangeRequest) -> None:
    kind = kinds.get(change.kind)
    try:
        with transaction.atomic():
            if kind is None:
                raise ChangeError(_("This kind of change is no longer available."))
            result_url = kind.apply(change) or ""
    except Exception as exc:
        logger.warning("Change request %s could not be applied: %s", change.pk, exc)
        messages = getattr(exc, "messages", None)
        error = "; ".join(str(m) for m in messages) if messages else str(exc)
        _close(change, ChangeRequest.Status.FAILED, error=error or exc.__class__.__name__)
        audit.record("changes.failed", change, message=change.error[:500])
        return
    _close(change, ChangeRequest.Status.APPLIED, result_url=result_url)
    audit.record(
        "changes.applied",
        change,
        changes={"kind": change.kind, "proposed_by": change.proposed_by_id},
        message=change.summary,
    )


@transaction.atomic
def approve(change: ChangeRequest, user, comment: str = "") -> ChangeRequest:
    change = _lock(change)
    if not can_decide(change, user):
        raise PermissionDenied(_("You can't approve this change."))
    ChangeDecision.all_associations.create(
        association=change.association,
        change=change,
        user=user,
        decision=ChangeDecision.Decision.APPROVE,
        comment=comment,
    )
    if change.approvals >= change.required_approvals:
        _apply(change)
        from . import notifications

        notifications.notify_proposer(change, decider=user)
    return change


@transaction.atomic
def reject(change: ChangeRequest, user, comment: str) -> ChangeRequest:
    change = _lock(change)
    if not can_decide(change, user):
        raise PermissionDenied(_("You can't reject this change."))
    if not (comment or "").strip():
        raise ChangeError(_("Say why you are rejecting it."))
    ChangeDecision.all_associations.create(
        association=change.association,
        change=change,
        user=user,
        decision=ChangeDecision.Decision.REJECT,
        comment=comment,
    )
    _close(change, ChangeRequest.Status.REJECTED)
    from . import notifications

    notifications.notify_proposer(change, decider=user, reason=comment)
    return change


@transaction.atomic
def withdraw(change: ChangeRequest, user) -> ChangeRequest:
    change = _lock(change)
    if not change.is_pending or user.pk != change.proposed_by_id:
        raise PermissionDenied(_("Only the person who proposed it can withdraw it."))
    _close(change, ChangeRequest.Status.WITHDRAWN)
    return change


def pending(association):
    return ChangeRequest.all_associations.filter(
        association=association, status=ChangeRequest.Status.PENDING
    )
