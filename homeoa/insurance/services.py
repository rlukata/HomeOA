"""Master policy lookups for the Insurance page, the owner Docs tab and the compliance
calendar."""

from __future__ import annotations

import datetime as dt

from django.db import transaction

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm

from .models import MasterPolicy


def can_manage(user, association) -> bool:
    return has_perm(user, perms.DOCUMENTS_MANAGE, association=association)


def current(association, on: dt.date) -> MasterPolicy | None:
    """The term covering ``on``; else the latest term that has started (lapsed, so the
    board sees it); else the next one."""
    policies = list(MasterPolicy.all_associations.filter(association=association))
    covering = [p for p in policies if p.covers(on)]
    if covering:
        return max(covering, key=lambda p: p.start_date)
    started = [p for p in policies if p.start_date <= on]
    if started:
        return max(started, key=lambda p: p.end_date)
    return min(policies, key=lambda p: p.start_date, default=None)


def renewal_due(association, start: dt.date, end: dt.date) -> list[MasterPolicy]:
    """Terms ending in ``start``..``end`` with no later term on file yet."""
    policies = list(MasterPolicy.all_associations.filter(association=association))
    latest_end = max((p.end_date for p in policies), default=None)
    return [p for p in policies if start <= p.end_date <= end and p.end_date == latest_end]


@transaction.atomic
def save(policy: MasterPolicy) -> MasterPolicy:
    """Create or change a term. Audited through the model."""
    policy.full_clean()
    policy.save()
    return policy


def owner_summary(user, association, on: dt.date) -> dict | None:
    """What an owner sees on Docs: carrier, term, deductible and the certificate if owners
    may open it. ``None`` for tenants or when no policy is on file."""
    from homeoa.documents import services as docs

    if not has_perm(user, perms.BALANCE_OWN, association=association):
        return None
    policy = current(association, on)
    if policy is None:
        return None
    doc = policy.certificate
    if doc is not None and not (
        doc.current_version_id and docs.can_view(user, association, doc.visibility)
    ):
        doc = None
    return {"policy": policy, "certificate": doc}
