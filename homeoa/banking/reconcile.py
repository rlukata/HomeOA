"""Bank reconciliation (FIN-07).

A reconciliation compares the bank statement with the transactions HomeOA has accounted for:

    difference = ending balance - (beginning balance + cleared items)

* Beginning balance: the previous completed reconciliation's ending balance; for the first
  one, the GL cash balance (the bank account's GL account and fund) just before the first
  unreconciled transaction. The treasurer can override it on the first reconciliation.
* Cleared items: transactions of the account dated on or before the statement date that
  are not in an earlier reconciliation and have been matched, categorized or ignored.
  Unmatched ones are listed separately; they count only once accounted for.
* ``complete`` refuses unless the difference is $0.00, then locks the cleared
  transactions (matching can no longer change or undo them).

Books check (``book_check``): book cash (GL account + fund) as of the statement date, minus
outstanding items (ledger cash lines after ``book_start`` up to the statement date whose entry
isn't tied to a matched bank transaction dated on or before it) = adjusted book balance;
``book difference = ending balance - adjusted book balance``. A non-zero book difference does
not block completion, but needs a written note, kept on the reconciliation and in the audit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.core.fields import ZERO
from homeoa.ledger.models import JournalLine
from homeoa.ledger.services import account_balance
from homeoa.payments.models import Receipt, ReceiptStatus

from .models import BankAccount, BankTransaction, Match, Reconciliation, TxnStatus


class ReconcileRefused(ValueError):
    """``str(err)`` is user-facing."""


def last_completed(bank_account: BankAccount) -> Reconciliation | None:
    return (
        Reconciliation.all_associations.filter(
            bank_account=bank_account, completed_at__isnull=False
        )
        .order_by("-statement_date", "-pk")
        .first()
    )


def _open_txns(bank_account, statement_date):
    return BankTransaction.all_associations.filter(
        bank_account=bank_account, reconciliation__isnull=True, date__lte=statement_date
    )


def _first_as_of(bank_account, statement_date) -> dt.date:
    first = (
        BankTransaction.all_associations.filter(
            bank_account=bank_account, reconciliation__isnull=True
        )
        .order_by("date")
        .values_list("date", flat=True)
        .first()
    )
    return statement_date if first is None or first > statement_date else first - dt.timedelta(1)


def default_beginning(bank_account: BankAccount, statement_date: dt.date) -> Decimal:
    prev = last_completed(bank_account)
    if prev is not None:
        return prev.ending_balance
    as_of = _first_as_of(bank_account, statement_date)
    return account_balance(bank_account.gl_account, as_of=as_of, fund=bank_account.fund)


@dataclass
class BookCheck:
    book_balance: Decimal
    outstanding: list  # JournalLine rows, ``amount`` signed (debit positive)
    outstanding_total: Decimal
    adjusted: Decimal
    difference: Decimal


def book_check(rec: Reconciliation) -> BookCheck:
    acct = rec.bank_account
    book = account_balance(acct.gl_account, as_of=rec.statement_date, fund=acct.fund)
    tied = Match.all_associations.filter(
        association=rec.association,
        undone_at__isnull=True,
        entry__isnull=False,
        txn__bank_account=acct,
        txn__date__lte=rec.statement_date,
    ).values("entry_id")
    voided = Receipt.all_associations.filter(association=rec.association, status=ReceiptStatus.VOID)
    lines = (
        JournalLine.all_associations.filter(
            association=rec.association,
            account=acct.gl_account,
            fund=acct.fund,
            entry__date__lte=rec.statement_date,
        )
        .exclude(entry_id__in=tied)
        # A reversed entry and its reversal cancel out; neither is outstanding.
        .exclude(entry__reversed_by__isnull=False)
        .exclude(entry__reversal_of__isnull=False)
        # Likewise a voided receipt (undone bank match) and its void entry.
        .exclude(entry_id__in=voided.values("entry_id"))
        .exclude(entry_id__in=voided.values("void_entry_id"))
        .select_related("entry")
        .order_by("entry__date", "pk")
    )
    if rec.book_start:
        lines = lines.filter(entry__date__gt=rec.book_start)
    lines = list(lines)
    total = sum((ln.amount for ln in lines), ZERO)
    adjusted = book - total
    return BookCheck(book, lines, total, adjusted, rec.ending_balance - adjusted)


def cleared_items(rec: Reconciliation):
    if rec.is_complete:
        return rec.cleared.all()
    return _open_txns(rec.bank_account, rec.statement_date).exclude(status=TxnStatus.UNMATCHED)


def unmatched_items(rec: Reconciliation):
    if rec.is_complete:
        return BankTransaction.all_associations.none()
    return _open_txns(rec.bank_account, rec.statement_date).filter(status=TxnStatus.UNMATCHED)


def refresh(rec: Reconciliation) -> Reconciliation:
    """Recompute ``cleared_total`` for a draft from the current transactions."""
    if not rec.is_complete:
        rec.cleared_total = cleared_items(rec).aggregate(s=Sum("amount"))["s"] or ZERO
    return rec


def start(
    bank_account: BankAccount,
    statement_date: dt.date,
    ending_balance: Decimal,
    *,
    beginning_balance: Decimal | None = None,
    user=None,
) -> Reconciliation:
    """Create a draft (replacing any earlier draft for the account)."""
    if not isinstance(ending_balance, Decimal):
        raise TypeError("ending_balance must be a Decimal")
    prev = last_completed(bank_account)
    if prev is not None and statement_date <= prev.statement_date:
        raise ReconcileRefused(
            _("This account is already reconciled through %(d)s.")
            % {"d": prev.statement_date.isoformat()}
        )
    book_start = prev.book_start if prev else _first_as_of(bank_account, statement_date)
    if prev is not None:
        beginning_balance = prev.ending_balance  # carried forward, not editable
    elif beginning_balance is None:
        beginning_balance = default_beginning(bank_account, statement_date)
    actor = user if getattr(user, "pk", None) else None
    with transaction.atomic():
        Reconciliation.all_associations.filter(
            bank_account=bank_account, completed_at__isnull=True
        ).delete()
        rec = Reconciliation(
            association=bank_account.association,
            bank_account=bank_account,
            statement_date=statement_date,
            beginning_balance=beginning_balance,
            ending_balance=ending_balance,
            book_start=book_start,
        )
        refresh(rec)
        rec.save()
        audit.record(
            "bank.reconcile_start",
            rec,
            actor=actor,
            changes={
                "statement_date": statement_date.isoformat(),
                "beginning": f"{beginning_balance:.2f}",
                "ending": f"{ending_balance:.2f}",
            },
        )
    return rec


def complete(rec: Reconciliation, user, *, note: str = "") -> Reconciliation:
    actor = user if getattr(user, "pk", None) else None
    with transaction.atomic():
        rec = Reconciliation.all_associations.select_for_update().get(pk=rec.pk)
        if rec.is_complete:
            raise ReconcileRefused(_("This reconciliation is already complete."))
        refresh(rec)
        if rec.difference != 0:
            raise ReconcileRefused(
                _("The difference is %(d)s. It must be $0.00 to finish.")
                % {"d": f"${rec.difference:,.2f}"}
            )
        books = book_check(rec)
        note = (note or "").strip()
        if books.difference != 0 and not note:
            raise ReconcileRefused(
                _(
                    "The books differ from the bank by %(d)s. Write a note explaining why "
                    "before finishing."
                )
                % {"d": f"${books.difference:,.2f}"}
            )
        rec.book_note = note
        ids = list(cleared_items(rec).values_list("pk", flat=True))
        # Only the lock field changes; source fields stay immutable.
        BankTransaction.all_associations.filter(pk__in=ids).update(reconciliation=rec)
        rec.completed_at = timezone.now()
        rec.completed_by = actor
        rec.save()
        audit.record(
            "bank.reconcile",
            rec,
            actor=actor,
            changes={
                "statement_date": rec.statement_date.isoformat(),
                "ending": f"{rec.ending_balance:.2f}",
                "cleared": len(ids),
                "book_difference": f"{books.difference:.2f}",
                "note": note,
            },
        )
    return rec
