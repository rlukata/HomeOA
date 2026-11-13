"""Two board members for money leaving a reserve bank account.

Where the rule pack requires it (``reserves.spend_min_approvals`` of 2 or more; WA: two
signatures for any transaction affecting reserve funds, RCW 64.34.372(2)), every debit on
a reserve-fund bank account needs a :class:`ReserveApproval`. Bills already meet this
through their own approvals (``ap.approvals.rules.reserve_min_approvals``); everything
else (a transfer to another fund, a categorized fee) is covered here.

HomeOA never moves money: the board asks for approval first (Money > Bank & deposits >
Approve a reserve withdrawal, a change request that a second board member approves), then
makes the transfer at the bank. When the bank debit is matched or categorized, it uses up
the oldest unused approval of the same amount. A debit matched with no approval can still
be matched, but stays on Home as a to-do until a second board member approves it after
the fact.
"""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa import rules as rule_values

from .models import BankTransaction, Match, MatchKind, ReserveApproval, TxnStatus

DONE = (TxnStatus.MATCHED, TxnStatus.CATEGORIZED)


def required(association, on=None) -> bool:
    """True when money leaving a reserve account needs two board members."""
    value = rule_values.value(association, "reserves.spend_min_approvals", on)
    return int(value or 0) >= 2


def is_reserve_debit(txn: BankTransaction) -> bool:
    return txn.amount < 0 and txn.bank_account.fund.is_reserve


def _paid_bill_with_reserve_lines(txn: BankTransaction) -> bool:
    """A bill payment whose bill had a reserve line was already approved under the
    reserve rule when the bill was approved."""
    from homeoa.ap.approvals.rules import reserve_min_approvals

    bills = [
        m.bill
        for m in Match.all_associations.filter(
            txn=txn, kind=MatchKind.BILL, undone_at__isnull=True
        ).select_related("bill")
    ]
    return bool(bills) and all(reserve_min_approvals(b) for b in bills)


def covered(txn: BankTransaction) -> bool:
    if ReserveApproval.all_associations.filter(txn=txn).exists():
        return True
    return _paid_bill_with_reserve_lines(txn)


def unused(association):
    return ReserveApproval.all_associations.filter(association=association, txn__isnull=True)


def status_changed(txn: BankTransaction, user=None) -> None:
    """Called whenever matching changes a transaction's status (inside its transaction).

    A reserve debit that becomes matched or categorized takes the oldest unused approval of
    the same amount; one that goes back to unmatched gives an approval made before the
    move back, so the next match can use it. Approvals made after the fact stay with their
    transaction.
    """
    if not is_reserve_debit(txn):
        return
    if txn.status in DONE:
        if covered(txn) or not required(txn.association, txn.date):
            return
        approval = (
            unused(txn.association)
            .filter(amount=-txn.amount)
            .select_for_update()
            .order_by("created_at", "pk")
            .first()
        )
        if approval is not None:
            approval.txn = txn
            approval.save(update_fields=["txn"])
            audit.record(
                "bank.reserve_approval_used", approval, actor=user, changes={"txn": txn.pk}
            )
    elif txn.status == TxnStatus.UNMATCHED:
        approval = ReserveApproval.all_associations.filter(txn=txn, after_the_fact=False).first()
        if approval is not None:
            approval.txn = None
            approval.save(update_fields=["txn"])
            audit.record(
                "bank.reserve_approval_released", approval, actor=user, changes={"txn": None}
            )


def unapproved(association):
    """Matched or categorized reserve debits with no approval behind them."""
    txns = (
        BankTransaction.all_associations.filter(
            association=association,
            amount__lt=0,
            bank_account__fund__is_reserve=True,
            status__in=DONE,
            reserve_approval__isnull=True,
        )
        .select_related("bank_account__fund")
        .order_by("date", "pk")
    )
    return [t for t in txns if required(association, t.date) and not covered(t)]


def approve(association, *, amount, purpose, user, txn=None) -> ReserveApproval:
    """Record an approval (runs when its change request is approved)."""
    approval = ReserveApproval.all_associations.create(
        association=association,
        amount=amount,
        purpose=purpose,
        txn=txn,
        after_the_fact=txn is not None,
        created_by=user if getattr(user, "pk", None) else None,
    )
    audit.record(
        "bank.reserve_approval",
        approval,
        actor=approval.created_by,
        changes={"amount": str(amount), "txn": txn.pk if txn else None},
        message=purpose,
    )
    return approval


def todo_items(association) -> list[dict]:
    """ "Move $X out of reserve at the bank" for each approval not used yet, and a "needs a
    second approval" item for each reserve debit matched without one."""
    items = []
    for a in unused(association):
        items.append(
            {
                "id": f"reserve-move-{a.pk}",
                "kind": "money",
                "title": _("Move $%(amount)s out of the reserve account at the bank")
                % {"amount": f"{a.amount:,.2f}"},
                "sub": _("Approved: %(purpose)s. Match the bank transaction when it arrives.")
                % {"purpose": a.purpose},
                "url": reverse("banking:reserve"),
                "action": _("Review"),
            }
        )
    for txn in unapproved(association):
        items.append(
            {
                "id": f"reserve-unapproved-{txn.pk}",
                "kind": "money",
                "title": _("Reserve withdrawal without two approvals: $%(amount)s")
                % {"amount": f"{-txn.amount:,.2f}"},
                "sub": _("%(date)s · %(description)s. Ask a second board member to approve it.")
                % {"date": txn.date.isoformat(), "description": txn.description},
                "due": txn.date,
                "url": f"{reverse('banking:reserve')}?txn={txn.pk}",
                "action": _("Ask for approval"),
            }
        )
    return items
