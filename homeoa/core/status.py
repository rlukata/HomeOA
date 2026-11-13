"""Association status: Setting up, then Live from the go-live date.

Going live opens resident access. The System admin can still pause resident access in an
emergency, with a reason. A missing status row means Setting up and not paused; rows are
created when something is first set.
"""

from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .context import association_today
from .models import Association, AssociationStatus

SETTING_UP = _("Setting up")
LIVE = _("Live")


def status_for(association: Association) -> AssociationStatus:
    """The association's status row, or an unsaved default (Setting up, not paused).

    Read through the one-to-one accessor, so it is cached on the association instance."""
    try:
        return association.status
    except AssociationStatus.DoesNotExist:
        return AssociationStatus(association=association)


def _row_for_update(association: Association) -> AssociationStatus:
    row, _created = AssociationStatus.all_associations.select_for_update().get_or_create(
        association=association
    )
    association.status = row
    return row


def go_live_date(association: Association) -> datetime.date | None:
    return status_for(association).go_live_date


def is_live(association: Association, on: datetime.date | None = None) -> bool:
    """Whether the go-live date is set and has come by ``on`` (default: the association's
    today)."""
    go_live = go_live_date(association)
    if go_live is None:
        return False
    return go_live <= (on or association_today(association))


def label(association: Association, on: datetime.date | None = None):
    return LIVE if is_live(association, on) else SETTING_UP


def residents_open(association: Association | None) -> bool:
    """Live and resident access not paused."""
    if association is None:
        return False
    return is_live(association) and not status_for(association).residents_paused


def _require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required.")
    return reason


@transaction.atomic
def set_go_live_date(association: Association, date: datetime.date | None, *, reason: str) -> bool:
    """Set the go-live date (service only: data loading and tests). Audited; returns whether
    anything changed."""
    from homeoa import audit

    reason = _require_reason(reason)
    row = _row_for_update(association)
    old = row.go_live_date
    if old == date:
        return False
    row.go_live_date = date
    row.save(update_fields=["go_live_date"])
    audit.record(
        "association.go_live_date",
        association,
        changes={
            "go_live_date": [old.isoformat() if old else None, date.isoformat() if date else None],
            "reason": reason,
        },
    )
    return True


GO_LIVE_REASON = "Went live on the Go live page"


class AlreadyLive(Exception):
    """Going live was asked for an association that is already live."""


def go_live_today(association: Association, *, by=None) -> datetime.date:
    """Make the association live today, audited under ``by``. A future go-live date set
    earlier is replaced with today. Refuses when the association is already live, since
    going live cannot be undone or repeated. Returns the go-live date."""
    from homeoa.audit.context import audit_context, get_audit_actor

    today = association_today(association)
    with transaction.atomic():
        _row_for_update(association)
        if is_live(association, today):
            raise AlreadyLive(_("The association is already live."))
        if by is None:
            set_go_live_date(association, today, reason=GO_LIVE_REASON)
        else:
            # Keep the request's address and id; only the person is set here.
            current = get_audit_actor()
            with audit_context(
                actor=by, ip=current.ip, request_id=current.request_id, label=current.label
            ):
                set_go_live_date(association, today, reason=GO_LIVE_REASON)
    return today


@transaction.atomic
def _set_paused(association: Association, paused: bool, *, reason: str, by) -> bool:
    from homeoa import audit

    reason = _require_reason(reason)
    row = _row_for_update(association)
    if row.residents_paused == paused:
        return False
    was_open = residents_open(association)
    row.residents_paused = paused
    row.pause_reason = reason if paused else ""
    row.paused_by = by if paused else None
    row.paused_at = timezone.now() if paused else None
    row.save(update_fields=["residents_paused", "pause_reason", "paused_by", "paused_at"])
    audit.record(
        "association.residents_enabled",
        association,
        changes={
            "residents_enabled": [was_open, residents_open(association)],
            "residents_paused": [not paused, paused],
            "reason": reason,
        },
    )
    return True


def pause_residents(association: Association, *, reason: str, by=None) -> bool:
    """Close resident access (emergency switch). Returns whether anything changed."""
    return _set_paused(association, True, reason=reason, by=by)


def resume_residents(association: Association, *, reason: str, by=None) -> bool:
    """Lift a pause on resident access. Returns whether anything changed."""
    return _set_paused(association, False, reason=reason, by=by)


def setup_start(association: Association) -> datetime.date | None:
    """The go-live date while the association is still setting up, else ``None``. New dues
    schedules and late-fee policies default to starting then."""
    go_live = go_live_date(association)
    if go_live is None or is_live(association):
        return None
    return go_live
