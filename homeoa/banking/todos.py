"""Board to-do items for unmatched bank transactions. The Home to-do registry renders them.

``todo_items(association)`` returns dicts shaped like the prototype's to-dos (``id``,
``kind``, ``title``, ``sub``, ``url``, ``action``) plus ``post_url`` and ``post_data`` for a
one-click confirm of the top suggestion (a POST needing ``finance.manage``):

* "Match deposit: <description>" when the best suggestion is a unit or a recorded payment;
* "Categorize deposit: <description>" otherwise (``action`` "Categorize" opens the form).
"""

from django.urls import reverse
from django.utils.translation import gettext as _

from . import matching
from .models import BankTransaction, TxnStatus


def _money(d):
    return f"${d:,.2f}"


def todo_items(association) -> list[dict]:
    items = []
    txns = BankTransaction.all_associations.filter(
        association=association, status=TxnStatus.UNMATCHED, amount__gt=0
    ).order_by("date", "pk")
    for txn in txns:
        url = reverse("banking:txn", args=[txn.pk])
        best = next(iter(matching.suggestions(txn)), None)
        if best is not None and best.kind == "clearing":
            items.append(
                {
                    "id": f"bank-match-{txn.pk}",
                    "kind": "money",
                    "title": _("Match deposit: %(d)s") % {"d": txn.description},
                    "sub": _("%(amount)s → %(reason)s")
                    % {"amount": _money(txn.amount), "reason": best.reason},
                    "due": txn.date,
                    "url": url,
                    "action": _("Confirm"),
                    "post_url": reverse("banking:confirm", args=[txn.pk]),
                    "post_data": {"clearing": best.extra.get("key", "")},
                }
            )
        elif best is not None and best.kind in ("unit", "link") and best.unit is not None:
            items.append(
                {
                    "id": f"bank-match-{txn.pk}",
                    "kind": "money",
                    "title": _("Match deposit: %(d)s") % {"d": txn.description},
                    "sub": _("%(amount)s → %(unit)s (%(reason)s)")
                    % {"amount": _money(txn.amount), "unit": best.unit, "reason": best.reason},
                    "due": txn.date,
                    "url": url,
                    "action": _("Confirm"),
                    "post_url": reverse("banking:confirm", args=[txn.pk]),
                    "post_data": {"unit": best.unit.pk}
                    if best.kind == "unit"
                    else {"receipt": best.receipt.pk},
                }
            )
        else:
            sub = _money(txn.amount)
            if best is not None and best.account is not None:
                sub = _("%(amount)s · suggested: %(account)s") % {
                    "amount": sub,
                    "account": best.account.name,
                }
            items.append(
                {
                    "id": f"bank-categorize-{txn.pk}",
                    "kind": "money",
                    "title": _("Categorize deposit: %(d)s") % {"d": txn.description},
                    "sub": sub,
                    "due": txn.date,
                    "url": url,
                    "action": _("Categorize"),
                }
            )
    return items
