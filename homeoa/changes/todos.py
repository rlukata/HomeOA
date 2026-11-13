"""Home to-dos: one "Approve change" item per pending change request per board member who
can still approve it, shown only to that person."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from . import services


def todo_items(association) -> list[dict]:
    items = []
    for change in services.pending(association).select_related("proposed_by", "association"):
        done = change.approvals
        for user in services.waiting_on(change):
            items.append(
                {
                    "id": f"change-approve-{change.pk}-{user.pk}",
                    "kind": "gov",
                    "title": _("Approve change: %(summary)s") % {"summary": change.summary},
                    "sub": _("Proposed by %(name)s · %(done)s of %(required)s approvals")
                    % {
                        "name": change.proposed_by.full_name or change.proposed_by.email,
                        "done": done,
                        "required": change.required_approvals,
                    },
                    "url": reverse("changes:detail", args=[change.pk]),
                    "action": _("Review"),
                    "for_user": user.pk,
                }
            )
    return items
