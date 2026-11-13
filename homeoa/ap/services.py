"""Bill lifecycle. Every ledger change goes through ``homeoa.ledger.services``.

* ``submit(bill, user)``: draft -> pending/approved. Posts the accrual on the bill date:
  Dr expense per line (in the line's fund) / Cr 2000 Accounts payable per fund. Then asks
  ``ap.approvals.required(bill)`` for the resulting status.
* ``void(bill, user, reason)``: a draft just becomes void; a submitted, unpaid bill also
  gets the reversal of its accrual (dated today). Paid bills cannot be voided.
* ``mark_paid(bill, user, paid_on, bank_accounts)``: manual record of a payment made at
  the bank: Dr AP / Cr the bank account's cash account, per fund. Needs
  ``finance.manage`` and is audited. Bank matching calls it for a matched debit.
* ``reverse_payment(bill, user)``: undo of a payment (bank unmatch): reverses the payment
  entry and puts the bill back to approved.

All errors are :class:`BillError` (a ``ValueError``) with a translated message.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.services import has_any, has_perm
from homeoa.core.context import association_today
from homeoa.core.fields import ZERO
from homeoa.ledger import services as ledger
from homeoa.ledger.models import Account, AccountType, Fund

from . import approvals
from .models import Bill, BillAttachment

AP_ACCOUNT = "2000"
SOURCE = "ap.bill"
SOURCE_PAYMENT = "ap.payment"
ATTACHMENT_FOLDER = "bills"


class BillError(ValueError):
    pass


def _require_manage(user, bill) -> None:
    if user is not None and not has_perm(user, perms.FINANCE_MANAGE, association=bill.association):
        raise PermissionDenied


def _require_enter(user, bill) -> None:
    """Entering and submitting a bill: anyone who enters bills (its approval rules decide
    whether it may be paid)."""
    if user is None:
        return
    if not has_any(user, (perms.FINANCE_MANAGE, perms.BILLS_ENTER), association=bill.association):
        raise PermissionDenied


def _lock(bill: Bill) -> Bill:
    return Bill.all_associations.select_for_update().get(pk=bill.pk)


def fund_totals(bill: Bill) -> dict[Fund, Decimal]:
    totals: dict[Fund, Decimal] = defaultdict(lambda: ZERO)
    for ln in bill.lines.select_related("fund"):
        totals[ln.fund] += ln.amount
    return dict(totals)


def accrual_lines(bill: Bill) -> list[ledger.Line]:
    memo = bill.description or str(bill)
    lines = [
        ledger.Line(account=ln.account, fund=ln.fund, debit=ln.amount, memo=ln.memo or memo)
        for ln in bill.lines.select_related("account", "fund")
    ]
    for fund, amount in fund_totals(bill).items():
        lines.append(ledger.Line(account=AP_ACCOUNT, fund=fund, credit=amount, memo=memo))
    return lines


def _validate_lines(bill: Bill) -> None:
    lines = list(bill.lines.select_related("account"))
    if not lines:
        raise BillError(_("A bill needs at least one line."))
    for ln in lines:
        if ln.amount is None or ln.amount <= 0:
            raise BillError(_("Every line needs a positive amount."))
        if ln.account.type != AccountType.EXPENSE:
            raise BillError(_("%(account)s is not an expense account.") % {"account": ln.account})


def submit(bill: Bill, user=None) -> Bill:
    """Post the accrual and route the bill to approval. See the module docstring."""
    _require_enter(user, bill)
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status != Bill.Status.DRAFT:
            raise BillError(_("Only a draft bill can be submitted."))
        _validate_lines(bill)
        try:
            entry = ledger.post_entry(
                accrual_lines(bill),
                date=bill.bill_date,
                memo=_("Bill %(bill)s") % {"bill": bill},
                source_type=SOURCE,
                source_id=bill.pk,
                posted_by=user,
                association=bill.association,
            )
        except ledger.LedgerError as exc:
            raise BillError(str(exc)) from exc
        now = timezone.now()
        bill.entry = entry
        bill.submitted_at = now
        outcome = approvals.required(bill)
        if outcome == approvals.APPROVED:
            bill.status = Bill.Status.APPROVED
            bill.approved_at = now
        else:
            bill.status = Bill.Status.PENDING
        bill.save()
        audit.record("ap.bill_submitted", bill, actor=user, message=f"{bill.total:.2f}")
        if bill.status == Bill.Status.PENDING:
            from .approvals import notifications
            from .approvals.services import waiting_on

            notifications.notify_approvers(bill, waiting_on(bill), submitter=user)
    return bill


def void(bill: Bill, user=None, reason: str = "", *, date: dt.date | None = None) -> Bill:
    _require_manage(user, bill)
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status == Bill.Status.VOID:
            raise BillError(_("This bill is already void."))
        if bill.status == Bill.Status.PAID:
            raise BillError(_("A paid bill cannot be voided."))
        if bill.entry_id:
            try:
                bill.void_entry = ledger.reverse_entry(
                    bill.entry,
                    date=date or association_today(bill.association),
                    memo=_("Void of bill %(bill)s") % {"bill": bill},
                    posted_by=user,
                )
            except ledger.LedgerError as exc:
                raise BillError(str(exc)) from exc
        bill.status = Bill.Status.VOID
        bill.voided_at = timezone.now()
        bill.void_reason = reason[:255]
        bill.save()
        audit.record("ap.bill_voided", bill, actor=user, message=reason[:200])
    return bill


def mark_paid(
    bill: Bill, user, *, paid_on: dt.date, bank_accounts: dict, paid_by: str = Bill.PaidBy.BANK
) -> Bill:
    """Record a payment made at the bank. ``bank_accounts`` maps each of the bill's funds
    (``Fund`` or fund pk) to the ``banking.BankAccount`` the money left from; that account
    must be in the same fund. Posts Dr AP / Cr the account's GL cash account per fund."""
    _require_manage(user, bill)
    by_pk = {getattr(k, "pk", k): v for k, v in bank_accounts.items()}
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status != Bill.Status.APPROVED:
            raise BillError(_("Only an approved bill can be marked paid."))
        lines = []
        memo = _("Payment of bill %(bill)s") % {"bill": bill}
        for fund, amount in fund_totals(bill).items():
            bank = by_pk.get(fund.pk)
            if bank is None:
                raise BillError(_("Pick the bank account for the %(fund)s fund.") % {"fund": fund})
            if bank.fund_id != fund.pk or bank.association_id != bill.association_id:
                raise BillError(
                    _("%(bank)s is not in the %(fund)s fund.") % {"bank": bank, "fund": fund}
                )
            lines.append(ledger.Line(account=AP_ACCOUNT, fund=fund, debit=amount, memo=memo))
            lines.append(ledger.Line(account=bank.gl_account, fund=fund, credit=amount, memo=memo))
        try:
            entry = ledger.post_entry(
                lines,
                date=paid_on,
                memo=memo,
                source_type=SOURCE_PAYMENT,
                source_id=bill.pk,
                posted_by=user,
                association=bill.association,
            )
        except ledger.LedgerError as exc:
            raise BillError(str(exc)) from exc
        bill.payment_entry = entry
        bill.paid_on = paid_on
        bill.paid_by = paid_by if paid_by in Bill.PaidBy.values else Bill.PaidBy.BANK
        bill.status = Bill.Status.PAID
        bill.save()
        audit.record("ap.bill_marked_paid", bill, actor=user, message=paid_on.isoformat())
    return bill


def pay_from_one_bank(bill: Bill, user, *, paid_on: dt.date, bank_account) -> Bill:
    """One debit from ``bank_account`` (in fund P) pays a bill whose lines span
    several funds. One balanced entry: in P, Dr AP (P's share) and Dr due-from <F> for each
    other fund's share / Cr cash (total); in each other fund F, Dr AP (F's share) / Cr
    due-to <P>. Codes come from ``ledger.chart.interfund_codes``. A single-fund bill in P
    posts exactly what ``mark_paid`` posts. ``reverse_payment`` undoes it."""
    from homeoa.ledger.chart import interfund_codes

    _require_manage(user, bill)
    if bank_account.association_id != bill.association_id:
        raise BillError(_("That bank account can't pay this bill."))
    paying = bank_account.fund
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status != Bill.Status.APPROVED:
            raise BillError(_("Only an approved bill can be marked paid."))
        memo = _("Payment of bill %(bill)s") % {"bill": bill}
        lines = []
        total = ZERO
        for fund, amount in sorted(fund_totals(bill).items(), key=lambda kv: kv[0].pk):
            total += amount
            lines.append(ledger.Line(account=AP_ACCOUNT, fund=fund, debit=amount, memo=memo))
            if fund.pk == paying.pk:
                continue
            due_from, due_to = interfund_codes(paying, fund)
            lines.append(ledger.Line(account=due_from, fund=paying, debit=amount, memo=memo))
            lines.append(ledger.Line(account=due_to, fund=fund, credit=amount, memo=memo))
        lines.append(
            ledger.Line(account=bank_account.gl_account, fund=paying, credit=total, memo=memo)
        )
        try:
            entry = ledger.post_entry(
                lines,
                date=paid_on,
                memo=memo,
                source_type=SOURCE_PAYMENT,
                source_id=bill.pk,
                posted_by=user,
                association=bill.association,
            )
        except ledger.LedgerError as exc:
            raise BillError(str(exc)) from exc
        bill.payment_entry = entry
        bill.paid_on = paid_on
        bill.status = Bill.Status.PAID
        bill.save()
        audit.record("ap.bill_marked_paid", bill, actor=user, message=paid_on.isoformat())
    return bill


def reverse_payment(bill: Bill, user, *, date: dt.date | None = None) -> Bill:
    """Reverse ``mark_paid``: post the mirror of the payment entry; the bill is approved
    (open in AP) again."""
    _require_manage(user, bill)
    with transaction.atomic():
        bill = _lock(bill)
        if bill.status != Bill.Status.PAID or not bill.payment_entry_id:
            raise BillError(_("This bill is not paid."))
        try:
            ledger.reverse_entry(
                bill.payment_entry,
                date=date,
                memo=_("Undo payment of bill %(bill)s") % {"bill": bill},
                posted_by=user,
            )
        except ledger.LedgerError as exc:
            raise BillError(str(exc)) from exc
        old = bill.paid_on
        bill.payment_entry = None
        bill.paid_on = None
        bill.status = Bill.Status.APPROVED
        bill.save()
        audit.record(
            "ap.bill_payment_reversed", bill, actor=user, message=old.isoformat() if old else ""
        )
    return bill


def expense_accounts(association):
    return Account.all_associations.filter(
        association=association, type=AccountType.EXPENSE, active=True
    ).order_by("code")


def add_attachment(bill: Bill, upload, user=None) -> BillAttachment:
    from homeoa.providers import get_provider

    storage = get_provider("FileStorage", bill.association)
    name = getattr(upload, "name", "") or "invoice"
    content_type = getattr(upload, "content_type", "") or "application/octet-stream"
    stored = storage.put(
        bill.association_id, name, upload, content_type=content_type, folder=ATTACHMENT_FOLDER
    )
    attachment = BillAttachment.objects.create(
        association=bill.association,
        bill=bill,
        file_key=stored.key,
        name=name[:255],
        content_type=stored.content_type or content_type,
        size=stored.size,
        uploaded_by=user if getattr(user, "pk", None) else None,
    )
    audit.record("ap.bill_attachment_added", bill, actor=user, message=attachment.name)
    return attachment


def attachment_content(attachment: BillAttachment) -> bytes:
    from homeoa.providers import get_provider

    return get_provider("FileStorage", attachment.association).get(attachment.file_key)
