"""Open interfund balances and the Home to-do that asks the board to settle them.

Each fund pair has its own due-from / due-to accounts (``chart.interfund_codes``): 1900/2900
between operating and reserve, 1920/2920 for any pair involving insurance. The insurance
fund's own 1920/2920 balances mix every relationship it has, so a pair's balance is read in
the *other* fund (the "view" fund), where those accounts belong to that one relationship:
``due_from - due_to`` in the view fund is what the partner owes it (negative: the view fund
owes the partner).

Read-only: nothing here posts to the ledger. A matched interfund transfer
(``banking.matching.confirm_interfund``) moves the same accounts, so the balance, and the
to-do, shrink and clear on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

from django.db.models import Sum
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _

from .chart import INSURANCE_FUND, interfund_codes
from .models import Fund, JournalLine

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class InterfundBalance:
    debtor: Fund  # the fund that owes
    creditor: Fund  # the fund that is owed
    amount: Decimal  # always positive
    view_fund: Fund  # the fund whose due-from / due-to lines hold this pair
    due_from: str
    due_to: str


def _net(association, fund, code, as_of) -> Decimal:
    qs = JournalLine.all_associations.filter(association=association, fund=fund, account__code=code)
    if as_of is not None:
        qs = qs.filter(entry__date__lte=as_of)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


def open_interfund_balances(association, as_of=None) -> list[InterfundBalance]:
    """One row per fund pair with a non-zero net due-to/due-from balance (Decimal)."""
    funds = list(Fund.all_associations.filter(association=association).order_by("sort_order", "pk"))
    out = []
    for a, b in combinations(funds, 2):
        view, partner = (b, a) if a.code == INSURANCE_FUND else (a, b)
        due_from, due_to = interfund_codes(view, partner)
        # Both nets are debit minus credit, so their sum is due-from less due-to.
        owed_to_view = _net(association, view, due_from, as_of) + _net(
            association, view, due_to, as_of
        )
        if owed_to_view == 0:
            continue
        debtor, creditor = (partner, view) if owed_to_view > 0 else (view, partner)
        out.append(InterfundBalance(debtor, creditor, abs(owed_to_view), view, due_from, due_to))
    return out


def todo_items(association) -> list[dict]:
    """One "Settle $X: <fund A> owes <fund B>" item per open pair, linking to its GL."""
    items = []
    for bal in open_interfund_balances(association):
        query = urlencode({"account": bal.due_from, "fund": bal.view_fund.code})
        low, high = sorted((bal.debtor.code, bal.creditor.code))
        items.append(
            {
                "id": f"ledger-interfund-{low}-{high}",
                "kind": "money",
                "title": _("Settle $%(amount)s: %(debtor)s owes %(creditor)s")
                % {
                    "amount": f"{bal.amount:,.2f}",
                    "debtor": _(bal.debtor.name),
                    "creditor": _(bal.creditor.name),
                },
                "sub": _("Move the money between the fund bank accounts, then match the transfer."),
                "url": f"{reverse('reports:gl')}?{query}",
                "action": _("Review"),
            }
        )
    return items
