"""Enforcement services: fine schedule lookup, violation logging with photos, status."""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.documents import services as documents
from homeoa.documents.models import Category, Visibility

from .models import FineSchedule, Violation, ViolationPhoto


class EnforcementError(ValidationError):
    pass


def can_manage(user, association) -> bool:
    return has_perm(user, perms.VIOLATIONS_MANAGE, association=association)


def schedule_in_force(association, on: dt.date) -> FineSchedule | None:
    """The fine schedule in force on ``on``: latest ``effective_on`` not after it."""
    return (
        FineSchedule.all_associations.filter(association=association, effective_on__lte=on)
        .order_by("-effective_on", "-version")
        .first()
    )


def next_schedule_version(association) -> int:
    last = FineSchedule.all_associations.filter(association=association).aggregate(
        n=Max("version")
    )["n"]
    return (last or 0) + 1


@transaction.atomic
def add_photo(violation: Violation, *, filename: str, content: bytes, user=None):
    documents.check_photo(filename, len(content))
    version = documents.attach(
        violation.association,
        filename=filename,
        content=content,
        title=_("Violation photo: %(unit)s, %(date)s")
        % {"unit": violation.unit, "date": violation.observed_on.isoformat()},
        category=Category.OTHER,
        visibility=Visibility.BOARD,
        user=user,
    )
    return ViolationPhoto.all_associations.create(
        association=violation.association, violation=violation, document=version.document
    )


@transaction.atomic
def log_violation(
    association, *, unit, category, observed_on, description, photos=(), user=None
) -> Violation:
    """Log a violation; ``photos`` is a list of ``(filename, bytes)``; at least one."""
    if unit.association_id != association.pk or category.association_id != association.pk:
        raise EnforcementError(_("Unit or category belongs to another association."))
    if not category.active:
        raise EnforcementError(_("That category is no longer active."))
    if not photos:
        raise EnforcementError(_("Attach at least one photo."))
    for name, content in photos:
        documents.check_photo(name, len(content))
    violation = Violation(
        association=association,
        unit=unit,
        category=category,
        observed_on=observed_on,
        description=description.strip(),
        reported_by=user if getattr(user, "is_authenticated", False) else None,
    )
    violation.full_clean(exclude=["association"])
    violation.save()
    for name, content in photos:
        add_photo(violation, filename=name, content=content, user=user)
    return violation


def set_status(violation: Violation, status: str, *, note: str = "", user=None) -> bool:
    if status not in Violation.Status.values:
        raise EnforcementError(_("Unknown status."))
    if violation.status == status:
        return False
    old = violation.status
    violation.status = status
    violation.status_note = note[:300]
    violation.save(update_fields=["status", "status_note"])
    audit.record(
        "enforcement.status_changed",
        violation,
        actor=user,
        changes={"status": [old, status]},
        message=note[:300],
    )
    return True


def open_violations(association):
    return Violation.all_associations.filter(association=association, status=Violation.Status.OPEN)
