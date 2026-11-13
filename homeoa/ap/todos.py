"""Home to-dos for bills waiting for approval (FIN-12).

One item per pending bill per eligible approver who has not yet decided, so each approver
has their own "Approve bill" item. Each item carries ``for_user`` so Home shows it only to
that approver. It links to the bill and has no one-click action.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from .approvals import services as approvals
from .models import Bill


def _name(user) -> str:
    return user.full_name or user.email


def todo_items(association) -> list[dict]:
    items = []
    bills = (
        Bill.all_associations.filter(association=association, status=Bill.Status.PENDING)
        .select_related("vendor", "association")
        .order_by("due_date", "pk")
    )
    for bill in bills:
        done, required = approvals.progress(bill)
        url = reverse("ap:detail", args=[bill.pk])
        for user in approvals.waiting_on(bill):
            items.append(
                {
                    "id": f"ap-approve-{bill.pk}-{user.pk}",
                    "kind": "money",
                    "title": _("Approve bill: %(vendor)s $%(amount)s")
                    % {"vendor": bill.vendor.name, "amount": f"{bill.total:,.2f}"},
                    "sub": _("For %(name)s · %(done)s of %(required)s approvals")
                    % {"name": _name(user), "done": done, "required": required},
                    "due": bill.due_date,
                    "url": url,
                    "action": _("Review"),
                    "for_user": user.pk,
                }
            )
    return items
