"""Home to-dos: one "Vote on ARC request" item per reviewer who has not voted yet, and
one "ARC decision overdue" item per open request past its ``decide_by``."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.core.context import association_today

from . import services
from .models import ArcRequest


def todo_items(association) -> list[dict]:
    return overdue_items(association) + vote_items(association)


def overdue_items(association) -> list[dict]:
    today = association_today(association)
    reqs = ArcRequest.all_associations.filter(
        association=association, status__in=ArcRequest.WAITING_ON_BOARD, decide_by__lt=today
    ).select_related("unit")
    return [
        {
            "id": f"arc-overdue-{req.pk}",
            "kind": "requests",
            "title": _("ARC decision overdue: %(unit)s") % {"unit": req.unit},
            "sub": req.title,
            "due": req.decide_by,
            "url": reverse("arc:detail", args=[req.pk]),
            "action": _("Decide"),
        }
        for req in reqs
    ]


def vote_items(association) -> list[dict]:
    people = [p for p in services.reviewers(association) if p.user_id]
    if not people:
        return []
    items = []
    open_reqs = ArcRequest.all_associations.filter(
        association=association,
        status__in=[ArcRequest.Status.SUBMITTED, ArcRequest.Status.UNDER_REVIEW],
    ).select_related("unit")
    for req in open_reqs:
        voted = set(req.votes.values_list("reviewer_id", flat=True))
        for person in people:
            if person.pk in voted:
                continue
            items.append(
                {
                    "id": f"arc-vote-{req.pk}-{person.pk}",
                    "kind": "requests",
                    "title": _("Vote on ARC request %(unit)s") % {"unit": req.unit},
                    "sub": req.title,
                    "due": None,
                    "url": reverse("arc:detail", args=[req.pk]),
                    "action": _("Review"),
                    "for_user": person.user_id,
                }
            )
    return items
