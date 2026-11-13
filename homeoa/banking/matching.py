"""Deposit matching: suggestions, confirm, split, link, categorize, interfund, undo (FIN-08).

Nothing here touches the ledger except through ``post_entry`` / ``reverse_entry``.
Every write requires ``finance.manage`` at the view and is audited here.

``suggestions(txn) -> list[Suggestion]``  best first. Sources, by score:
    * a recorded receipt (``payments.receipt`` entry) for the same cash account, amount and
      a nearby date that no deposit is linked to yet (link, don't double-count);
    * units: exact amount equal to what the unit owes, and/or the payer name matching a
      current owner's name or a saved :class:`PayerAlias`;
    * Interfund: an opposite transaction of the same amount between the operating and reserve
      bank accounts -> reserve transfer settlement;
    * categorize: an active :class:`CategorizeRule`; else the account an earlier
      transaction from the same payer was categorized to; else, for a debit with no bill
      to pay, the default expense account of the vendor named in the bank text; else
      Other income for deposits.

``confirm_units(txn, [(unit, amount), ...], user)``  receipt(s) for one or more units (split).
``link_receipt(txn, receipt, user)``  link to an existing recorded receipt; posts nothing.
``categorize(txn, account, user, save_rule="")``  post to an income/expense account.
``confirm_interfund(txn_a, txn_b, user)``  settle the due-to/due-from with the cash move.
``confirm_bill(txn, bill, user)``  a debit pays an approved bill (FIN-12): ``ap.mark_paid``.
``link_bill_payment(txn, bill, user)``  a debit is a bill payment already recorded by hand
    (``ap.mark_paid``); links it, posts nothing (no double count).
``undo(txn, user)``  reverse what matching posted; refused once reconciled.

Outbound: a debit suggests approved bills (never pending, draft, paid or void) for
the exact amount, wholly in the bank account's fund, approved on or before the debit date;
the vendor's name (or remittance payee / legal name) in the bank text raises the score.
A bill already marked paid by hand from this bank account for the same amount, near the
date and not yet linked is offered as a link (``bill_link``), and a categorize suggestion
then carries a warning. A pending bill with the same amount and vendor name, dated on or
before the debit, only gets a hint (``bill_pending``): approve it first; no override.
HomeOA never initiates the payment; it only records the one the bank shows.

Service hook for payment plugins: ``register_suggester(name, fn)`` adds
``fn(txn) -> list[Suggestion]`` to ``suggestions``. A plugin proposes a deposit that settles
a clearing account (e.g. a Stripe payout, Dr cash / Cr 1060) as a ``kind="clearing"``
suggestion with ``account`` set to the clearing account and ``extra["key"]`` identifying it;
``extra["on_confirm"](match)`` runs inside the confirm transaction. ``confirm_clearing``
posts it; ``undo`` reverses it like a categorization.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.core.fields import ZERO
from homeoa.ledger.chart import interfund_codes
from homeoa.ledger.models import Account, AccountType, JournalEntry
from homeoa.ledger.services import Line, account_balance, post_entry, reverse_entry, unit_balance
from homeoa.payments.services import DUE_TO, PaymentError

from . import receipts, reserve
from .models import (
    BankAccount,
    BankTransaction,
    CategorizeRule,
    Match,
    MatchKind,
    PayerAlias,
    TxnStatus,
)

OTHER_INCOME = "4200"
TRANSFER_WINDOW_DAYS = 5
# How many recent categorizations are searched for one from the same payer.
HISTORY_LOOKBACK = 500

# Words banks put around a payer's name that say nothing about who paid.
STOPWORDS = frozenset(
    [
        "ZELLE",
        "FROM",
        "TO",
        "PAYMENT",
        "PMT",
        "DEPOSIT",
        "DEP",
        "ACH",
        "ONLINE",
        "TRANSFER",
        "XFER",
        "MOBILE",
        "CHECK",
        "CHK",
        "CREDIT",
        "DEBIT",
        "POS",
        "VENMO",
        "PAYPAL",
        "WEB",
        "ID",
        "REF",
        "INC",
        "LLC",
        "THE",
        "AND",
        "OF",
        "FOR",
    ]
)


class MatchRefused(ValueError):
    """``str(err)`` is user-facing."""


@dataclass
class Suggestion:
    kind: str  # "link" | "unit" | "interfund" | "categorize" | "clearing" | "bill"
    score: int
    reason: str
    amount: Decimal
    unit: object = None
    account: Account | None = None
    entry: JournalEntry | None = None
    receipt: object = None
    partner: BankTransaction | None = None
    extra: dict = field(default_factory=dict)


_suggesters: dict[str, object] = {}


def register_suggester(name: str, fn) -> None:
    """Add a plugin's ``fn(txn) -> list[Suggestion]`` (replaces one of the same name)."""
    _suggesters[name] = fn


def unregister_suggester(name: str) -> None:
    _suggesters.pop(name, None)


def _plugin_suggestions(txn) -> list[Suggestion]:
    import logging

    out = []
    for name, fn in list(_suggesters.items()):
        try:
            out.extend(fn(txn) or [])
        except Exception:
            logging.getLogger(__name__).exception("Match suggester %s failed", name)
    return out


# ------------------------------------------------------------------ names


def normalize(text: str) -> str:
    words = re.sub(r"[^A-Z ]+", " ", (text or "").upper()).split()
    return " ".join(w for w in words if w not in STOPWORDS and len(w) > 1)


def payer_text(txn: BankTransaction) -> str:
    return normalize(txn.payee) or normalize(txn.description)


def name_matches(payer: str, full_name: str) -> bool:
    """All of the owner's name words (at least first and last) appear in the payer text."""
    name = normalize(full_name).split()
    words = set(payer.split())
    return len(name) >= 2 and all(w in words for w in name)


# ------------------------------------------------------------------ suggestions


def _owners_by_unit(association, on):
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_owners

    units = Unit.all_associations.filter(association=association)
    return [(u, current_owners(u, on)) for u in units]


def _interfund_partner(txn):
    if txn.status != TxnStatus.UNMATCHED:
        return None
    other_funds = BankAccount.all_associations.filter(association=txn.association).exclude(
        fund=txn.bank_account.fund
    )
    window = dt.timedelta(days=TRANSFER_WINDOW_DAYS)
    return (
        BankTransaction.all_associations.filter(
            association=txn.association,
            bank_account__in=other_funds,
            amount=-txn.amount,
            status=TxnStatus.UNMATCHED,
            date__range=(txn.date - window, txn.date + window),
        )
        .order_by("date", "pk")
        .first()
    )


def suggestions(txn: BankTransaction) -> list[Suggestion]:
    assoc = txn.association
    out: list[Suggestion] = []
    amount = abs(txn.amount)
    if txn.amount > 0:
        for rcpt in receipts.linkable_receipts(txn):
            out.append(
                Suggestion(
                    kind="link",
                    score=100,
                    reason=_("same amount as a payment already recorded"),
                    amount=amount,
                    unit=rcpt.unit,
                    receipt=rcpt,
                )
            )
        payer = payer_text(txn)
        alias_units = set(
            PayerAlias.all_associations.filter(association=assoc, text=payer).values_list(
                "unit_id", flat=True
            )
        )
        for unit, owners in _owners_by_unit(assoc, txn.date):
            owed = unit_balance(unit.pk, association=assoc)
            exact = owed == amount
            named = unit.pk in alias_units or any(name_matches(payer, p.full_name) for p in owners)
            if exact and named:
                score, reason = 90, _("exact amount and name match")
            elif named:
                score, reason = 60, _("name match")
            elif exact:
                score, reason = 40, _("exact amount match")
            else:
                continue
            out.append(
                Suggestion(
                    kind="unit",
                    score=score,
                    reason=reason,
                    amount=amount,
                    unit=unit,
                    extra={"owed": owed, "alias": unit.pk in alias_units},
                )
            )
    elif txn.amount < 0:
        out.extend(bill_suggestions(txn))
    out.extend(_plugin_suggestions(txn))
    partner = _interfund_partner(txn)
    if partner is not None:
        out.append(
            Suggestion(
                kind="interfund",
                score=80,
                reason=_("same amount moved between operating and reserve accounts"),
                amount=amount,
                partner=partner,
            )
        )
    rule = _rule_for(txn)
    past = _last_categorized(txn) if rule is None else None
    vendor = (
        _vendor_named(txn)
        if rule is None
        and past is None
        and txn.amount < 0
        and not any(s.kind in ("bill", "bill_link") for s in out)
        else None
    )
    if rule is not None:
        out.append(
            Suggestion(
                kind="categorize",
                score=50,
                reason=_("rule: description contains “%(text)s”") % {"text": rule.contains},
                amount=amount,
                account=rule.account,
            )
        )
    elif past is not None:
        out.append(
            Suggestion(
                kind="categorize",
                score=45,
                reason=_("same payee, categorized this way on %(date)s")
                % {"date": date_format(past.txn.date, "M j, Y")},
                amount=amount,
                account=past.account,
            )
        )
    elif vendor is not None:
        out.append(
            Suggestion(
                kind="categorize",
                score=35,
                reason=_("usual account for %(vendor)s") % {"vendor": vendor.name},
                amount=amount,
                account=vendor.default_expense_account,
            )
        )
    elif txn.amount > 0 and not any(s.kind in ("unit", "link", "clearing") for s in out):
        acct = Account.all_associations.filter(association=assoc, code=OTHER_INCOME).first()
        if acct is not None:
            out.append(
                Suggestion(
                    kind="categorize",
                    score=10,
                    reason=_("not from an owner"),
                    amount=amount,
                    account=acct,
                )
            )
    if any(x.kind == "bill_link" for x in out):
        for x in out:
            if x.kind == "categorize":
                x.extra["warning"] = _(
                    "A bill payment for this amount is already recorded. Link it instead, "
                    "or the expense is counted twice."
                )
    # Stable: highest score first, then unit number order.
    out.sort(key=lambda s: (-s.score, s.unit.sort_key if s.unit else (9, 0, "")))
    return out


def vendor_name_score(payer: str, vendor) -> int:
    """How many of the vendor's significant name words appear in the bank text (best of
    name, payee name, legal name), or 0 when fewer than two words (or not all of a one-word
    name) match."""
    words = set(payer.split())
    best = 0
    for name in (vendor.name, vendor.payee_name, vendor.legal_name):
        parts = normalize(name).split()
        if not parts:
            continue
        hits = sum(1 for w in parts if w in words)
        if hits == len(parts) or hits >= 2:
            best = max(best, hits)
    return best


def _approved_on(bill) -> dt.date | None:
    if bill.approved_at is None:
        return None
    return timezone.localdate(bill.approved_at)


def payable_bills(txn: BankTransaction):
    """Approved bills a debit could pay: exact amount, approved on or before the debit
    date, in any fund mix: shares outside the bank's fund become interfund balances
    (a fund may have no bank account of its own)."""
    from homeoa.ap.models import Bill

    if txn.amount >= 0:
        return []
    amount = -txn.amount
    out = []
    bills = (
        Bill.all_associations.filter(association=txn.association, status=Bill.Status.APPROVED)
        .select_related("vendor")
        .prefetch_related("lines__fund")
    )
    for bill in bills:
        lines = list(bill.lines.all())
        if not lines:
            continue
        if sum((ln.amount for ln in lines), ZERO) != amount:
            continue
        approved = _approved_on(bill)
        if approved is None or approved > txn.date:
            continue
        out.append(bill)
    return out


def _linked_bill_ids(association):
    return Match.all_associations.filter(
        association=association, undone_at__isnull=True, bill__isnull=False
    ).values("bill_id")


def linkable_bill_payments(txn: BankTransaction):
    """Bills marked paid by hand whose payment entry credits this bank's cash account (in
    its fund) with the debit's amount, paid near the debit date, and not linked yet."""
    from homeoa.ap.models import Bill
    from homeoa.banking.receipts import LINK_WINDOW_DAYS

    if txn.amount >= 0:
        return Bill.all_associations.none()
    bank = txn.bank_account
    window = dt.timedelta(days=LINK_WINDOW_DAYS)
    return (
        Bill.all_associations.filter(
            association=txn.association,
            status=Bill.Status.PAID,
            paid_on__range=(txn.date - window, txn.date + window),
            payment_entry__lines__account=bank.gl_account,
            payment_entry__lines__fund=bank.fund,
            payment_entry__lines__credit=-txn.amount,
        )
        .exclude(pk__in=_linked_bill_ids(txn.association))
        .select_related("vendor")
        .distinct()
        .order_by("paid_on", "pk")
    )


def _pending_bill_hints(txn, payer) -> list[Suggestion]:
    from homeoa.ap.models import Bill

    amount = -txn.amount
    out = []
    pending = (
        Bill.all_associations.filter(
            association=txn.association, status=Bill.Status.PENDING, bill_date__lte=txn.date
        )
        .select_related("vendor")
        .prefetch_related("lines")
    )
    for bill in pending:
        lines = list(bill.lines.all())
        if not lines:
            continue
        if sum((ln.amount for ln in lines), ZERO) != amount:
            continue
        if not vendor_name_score(payer, bill.vendor):
            continue
        out.append(
            Suggestion(
                kind="bill_pending",
                score=30,
                reason=_("Matches bill #%(n)s (awaiting approval). Approve it first.")
                % {"n": bill.invoice_number},
                amount=amount,
                extra={"bill": bill},
            )
        )
    return out


def fund_split(bill) -> list[tuple]:
    """``[(fund, amount), ...]`` per fund of a bill, in fund order."""
    totals: dict = {}
    for ln in bill.lines.all():
        fund = ln.fund
        totals[fund] = totals.get(fund, ZERO) + ln.amount
    return sorted(totals.items(), key=lambda kv: (kv[0].sort_order, kv[0].pk))


def bill_suggestions(txn: BankTransaction) -> list[Suggestion]:
    payer = normalize(f"{txn.payee} {txn.description}")
    out = [
        Suggestion(
            kind="bill_link",
            score=100,
            reason=_("bill payment already recorded"),
            amount=-txn.amount,
            extra={"bill": bill},
        )
        for bill in linkable_bill_payments(txn)
    ]
    for bill in payable_bills(txn):
        named = vendor_name_score(payer, bill.vendor) > 0
        split = fund_split(bill)
        out.append(
            Suggestion(
                kind="bill",
                score=95 if named else 45,
                reason=_("exact amount and vendor name match")
                if named
                else _("exact amount of an approved bill"),
                amount=-txn.amount,
                extra={
                    "bill": bill,
                    "split": split
                    if any(f.pk != txn.bank_account.fund_id for f, _a in split)
                    else [],
                },
            )
        )
    out.extend(_pending_bill_hints(txn, payer))
    return out


def _rule_for(txn):
    text = f"{txn.payee} {txn.description}".upper()
    for rule in CategorizeRule.all_associations.filter(association=txn.association, active=True):
        if rule.contains.strip() and rule.contains.strip().upper() in text:
            return rule
    return None


def _last_categorized(txn) -> Match | None:
    """The latest categorization still in force of an earlier transaction from the same
    payer, in the same direction (money in or out), to an active income or expense account.
    The payer is compared after ``normalize``, so dates and reference numbers in the bank
    text don't matter."""
    payer = payer_text(txn)
    if not payer or not txn.amount:
        return None
    direction = {"txn__amount__gt": 0} if txn.amount > 0 else {"txn__amount__lt": 0}
    recent = (
        Match.all_associations.filter(
            association=txn.association,
            kind=MatchKind.CATEGORIZED,
            undone_at__isnull=True,
            account__active=True,
            account__type__in=(AccountType.INCOME, AccountType.EXPENSE),
            **direction,
        )
        .exclude(txn=txn)
        .select_related("txn", "account")
        .order_by("-txn__date", "-pk")[:HISTORY_LOOKBACK]
    )
    return next((m for m in recent if payer_text(m.txn) == payer), None)


def _vendor_named(txn):
    """The active vendor whose name best matches the bank text and that has an active
    default expense account, or ``None``."""
    from homeoa.vendors.models import Vendor

    payer = normalize(f"{txn.payee} {txn.description}")
    best, best_score = None, 0
    vendors = Vendor.all_associations.filter(
        association=txn.association,
        status=Vendor.Status.ACTIVE,
        default_expense_account__active=True,
    ).select_related("default_expense_account")
    for vendor in vendors.order_by("name", "pk"):
        score = vendor_name_score(payer, vendor)
        if score > best_score:
            best, best_score = vendor, score
    return best


# ------------------------------------------------------------------ writes


def _lock(txn) -> BankTransaction:
    txn = BankTransaction.all_associations.select_for_update().get(pk=txn.pk)
    if txn.is_locked:
        raise MatchRefused(_("This transaction is in a completed reconciliation."))
    return txn


def _require_unmatched(txn):
    if txn.status != TxnStatus.UNMATCHED:
        raise MatchRefused(_("This transaction is already matched or categorized. Undo it first."))


def _set_status(txn, status, user):
    old = txn.status
    txn.status = status
    txn.save(update_fields=["status"])
    audit.record("bank.status", txn, actor=_actor(user), changes={"status": [old, status]})
    reserve.status_changed(txn, _actor(user))


def _actor(user):
    return user if getattr(user, "pk", None) else None


def _learn_alias(txn, unit, user):
    payer = payer_text(txn)
    if not payer:
        return
    from homeoa.registry.services import current_owners

    if any(name_matches(payer, p.full_name) for p in current_owners(unit, txn.date)):
        return  # the owner's own name already matches
    PayerAlias.all_associations.update_or_create(
        association=txn.association,
        text=payer,
        defaults={"unit": unit, "created_by": _actor(user)},
    )


def confirm_units(txn: BankTransaction, allocations, user, *, reason: str = "") -> list[Match]:
    """Record owner receipt(s) for a deposit. ``allocations``: [(unit, Decimal amount)]."""
    allocations = [(u, Decimal(a)) for u, a in allocations if a]
    with transaction.atomic():
        txn = _lock(txn)
        _require_unmatched(txn)
        if txn.amount <= 0:
            raise MatchRefused(_("Only deposits can be matched to units."))
        if not allocations or any(a <= 0 for _u, a in allocations):
            raise MatchRefused(_("Each unit needs an amount above zero."))
        total = sum((a for _u, a in allocations), ZERO)
        if total != txn.amount:
            raise MatchRefused(
                _("The split adds up to %(t)s but the deposit is %(d)s.")
                % {"t": f"{total:,.2f}", "d": f"{txn.amount:,.2f}"}
            )
        matches = []
        for unit, amount in allocations:
            try:
                rcpt = receipts.record_bank_receipt(txn, unit, amount, user=_actor(user))
            except PaymentError as e:
                raise MatchRefused(str(e)) from e
            matches.append(
                Match.all_associations.create(
                    association=txn.association,
                    txn=txn,
                    kind=MatchKind.RECEIPT,
                    amount=amount,
                    unit=unit,
                    entry=rcpt.entry,
                    receipt=rcpt,
                    reason=reason[:200],
                    created_by=_actor(user),
                )
            )
        if len(allocations) == 1:
            _learn_alias(txn, allocations[0][0], user)
        _set_status(txn, TxnStatus.MATCHED, user)
        audit.record(
            "bank.match",
            txn,
            actor=_actor(user),
            changes={"units": [[str(u), f"{a:.2f}"] for u, a in allocations]},
        )
    return matches


def link_receipt(txn: BankTransaction, receipt, user) -> Match:
    with transaction.atomic():
        txn = _lock(txn)
        _require_unmatched(txn)
        if not receipts.linkable_receipts(txn).filter(pk=receipt.pk).exists():
            raise MatchRefused(_("That payment can't be linked to this deposit."))
        match = Match.all_associations.create(
            association=txn.association,
            txn=txn,
            kind=MatchKind.LINKED,
            amount=txn.amount,
            unit_id=receipt.unit_id,
            entry=receipt.entry,
            receipt=receipt,
            posted=False,
            created_by=_actor(user),
        )
        _set_status(txn, TxnStatus.MATCHED, user)
        audit.record("bank.link", txn, actor=_actor(user), changes={"receipt": receipt.pk})
    return match


def categorize(txn: BankTransaction, account: Account, user, *, save_rule: str = "") -> Match:
    if account.type not in (AccountType.INCOME, AccountType.EXPENSE) or not account.active:
        raise MatchRefused(_("Choose an active income or expense account."))
    with transaction.atomic():
        txn = _lock(txn)
        _require_unmatched(txn)
        bank = txn.bank_account
        amount = abs(txn.amount)
        cash = Line(account=bank.gl_account, fund=bank.fund, debit=amount)
        other = Line(account=account, fund=bank.fund, credit=amount)
        if txn.amount < 0:
            cash = Line(account=bank.gl_account, fund=bank.fund, credit=amount)
            other = Line(account=account, fund=bank.fund, debit=amount)
        entry = post_entry(
            [cash, other],
            date=txn.date,
            memo=txn.description[:255],
            source_type="banking.categorize",
            source_id=txn.pk,
            posted_by=_actor(user),
            association=txn.association,
        )
        match = Match.all_associations.create(
            association=txn.association,
            txn=txn,
            kind=MatchKind.CATEGORIZED,
            amount=amount,
            account=account,
            entry=entry,
            created_by=_actor(user),
        )
        if save_rule.strip():
            CategorizeRule.all_associations.create(
                association=txn.association, contains=save_rule.strip()[:100], account=account
            )
        _set_status(txn, TxnStatus.CATEGORIZED, user)
        audit.record("bank.categorize", txn, actor=_actor(user), changes={"account": str(account)})
    return match


def confirm_clearing(txn: BankTransaction, suggestion: Suggestion, user) -> Match:
    """Post a plugin's clearing suggestion: Dr the bank's cash account / Cr the clearing
    account (same fund), and record it as a categorized match to that account."""
    account = suggestion.account
    if suggestion.kind != "clearing" or account is None or txn.amount <= 0:
        raise MatchRefused(_("That suggestion can't be confirmed here."))
    with transaction.atomic():
        txn = _lock(txn)
        _require_unmatched(txn)
        if suggestion.amount != txn.amount:
            raise MatchRefused(_("The amounts don't match."))
        bank = txn.bank_account
        entry = post_entry(
            [
                Line(account=bank.gl_account, fund=bank.fund, debit=txn.amount),
                Line(account=account, fund=bank.fund, credit=txn.amount),
            ],
            date=txn.date,
            memo=(suggestion.reason or txn.description)[:255],
            source_type="banking.clearing",
            source_id=txn.pk,
            posted_by=_actor(user),
            association=txn.association,
        )
        match = Match.all_associations.create(
            association=txn.association,
            txn=txn,
            kind=MatchKind.CATEGORIZED,
            amount=txn.amount,
            account=account,
            entry=entry,
            created_by=_actor(user),
        )
        _set_status(txn, TxnStatus.MATCHED, user)
        audit.record("bank.clearing", txn, actor=_actor(user), changes={"account": str(account)})
        hook = suggestion.extra.get("on_confirm")
        if hook is not None:
            hook(match)
    return match


def confirm_bill(txn: BankTransaction, bill, user) -> Match:
    """Record that a debit paid ``bill``: ``ap.services.mark_paid`` posts Dr AP / Cr the
    bank's cash account in the bill's fund and sets it paid; the match links the two. A
    bill spanning several funds is paid in one entry by ``ap.pay_from_one_bank``.

    Only an approved bill can be matched (draft, pending, paid and void are refused)."""
    from homeoa.ap import services as ap
    from homeoa.ap.models import Bill

    with transaction.atomic():
        txn = _lock(txn)
        _require_unmatched(txn)
        if txn.amount >= 0:
            raise MatchRefused(_("Only payments out of the bank can pay a bill."))
        bill = Bill.all_associations.select_for_update().get(pk=bill.pk)
        if bill.association_id != txn.association_id:
            raise MatchRefused(_("That bill can't be matched to this transaction."))
        if bill.status != Bill.Status.APPROVED:
            raise MatchRefused(
                _("Only an approved bill can be matched. This bill is %(status)s.")
                % {"status": bill.get_status_display().lower()}
            )
        if not any(b.pk == bill.pk for b in payable_bills(txn)):
            raise MatchRefused(
                _(
                    "This bill doesn't match the payment: the amount, fund or approval "
                    "date is different."
                )
            )
        single = all(ln.fund_id == txn.bank_account.fund_id for ln in bill.lines.all())
        try:
            if single:
                bill = ap.mark_paid(
                    bill,
                    user,
                    paid_on=txn.date,
                    bank_accounts={txn.bank_account.fund_id: txn.bank_account},
                )
            else:  # Other funds' shares become due-to/due-from balances
                bill = ap.pay_from_one_bank(
                    bill, user, paid_on=txn.date, bank_account=txn.bank_account
                )
        except ap.BillError as e:
            raise MatchRefused(str(e)) from e
        match = Match.all_associations.create(
            association=txn.association,
            txn=txn,
            kind=MatchKind.BILL,
            amount=-txn.amount,
            bill=bill,
            entry=bill.payment_entry,
            created_by=_actor(user),
        )
        _set_status(txn, TxnStatus.MATCHED, user)
        audit.record("bank.bill", txn, actor=_actor(user), changes={"bill": bill.pk})
    return match


def link_bill_payment(txn: BankTransaction, bill, user) -> Match:
    """Link a debit to a bill payment recorded by hand; posts nothing (the manual payment
    entry already credited this bank's cash account)."""
    with transaction.atomic():
        txn = _lock(txn)
        _require_unmatched(txn)
        if not linkable_bill_payments(txn).filter(pk=bill.pk).exists():
            raise MatchRefused(_("That bill payment can't be linked to this transaction."))
        bill = type(bill).all_associations.get(pk=bill.pk)
        match = Match.all_associations.create(
            association=txn.association,
            txn=txn,
            kind=MatchKind.BILL,
            amount=-txn.amount,
            bill=bill,
            entry=bill.payment_entry,
            posted=False,
            created_by=_actor(user),
        )
        _set_status(txn, TxnStatus.MATCHED, user)
        audit.record("bank.bill_link", txn, actor=_actor(user), changes={"bill": bill.pk})
    return match


def confirm_interfund(txn: BankTransaction, partner: BankTransaction, user) -> list[Match]:
    """Interfund settlement for a real cash move between any two funds.

    Source fund Dr due-to / Cr its cash; destination fund Dr its cash / Cr due-from. The pair
    is 2900/1900 between operating and reserve and 2920/1920 for any pair involving the
    insurance fund (``ledger.chart.interfund_codes``); the mirror image works the same way.
    """
    with transaction.atomic():
        txn, partner = _lock(txn), _lock(partner)
        _require_unmatched(txn)
        _require_unmatched(partner)
        same_fund = txn.bank_account.fund_id == partner.bank_account.fund_id
        if txn.amount != -partner.amount or same_fund:
            raise MatchRefused(_("These two transactions aren't a transfer between funds."))
        out_txn, in_txn = (txn, partner) if txn.amount < 0 else (partner, txn)
        src, dst = out_txn.bank_account, in_txn.bank_account
        amount = in_txn.amount
        due_from, due_to = interfund_codes(src.fund, dst.fund)
        entry = post_entry(
            [
                Line(account=due_to, fund=src.fund, debit=amount),
                Line(account=src.gl_account, fund=src.fund, credit=amount),
                Line(account=dst.gl_account, fund=dst.fund, debit=amount),
                Line(account=due_from, fund=dst.fund, credit=amount),
            ],
            date=in_txn.date,
            memo=_("Transfer between funds"),
            source_type="banking.interfund",
            source_id=f"{out_txn.pk}-{in_txn.pk}",
            posted_by=_actor(user),
            association=txn.association,
        )
        matches = []
        for t in (out_txn, in_txn):
            matches.append(
                Match.all_associations.create(
                    association=t.association,
                    txn=t,
                    kind=MatchKind.INTERFUND,
                    amount=amount,
                    entry=entry,
                    created_by=_actor(user),
                )
            )
            _set_status(t, TxnStatus.MATCHED, user)
        audit.record("bank.interfund", entry, actor=_actor(user), message=f"{amount:.2f}")
    return matches


def undo(txn: BankTransaction, user) -> None:
    """Reverse what matching posted for ``txn`` and mark it unmatched again.

    An interfund settlement is undone for both of its transactions.
    """
    with transaction.atomic():
        txn = _lock(txn)
        active = list(Match.all_associations.filter(txn=txn, undone_at__isnull=True))
        if not active and txn.status != TxnStatus.IGNORED:
            raise MatchRefused(_("Nothing to undo."))
        posted = [m for m in active if m.posted]
        rcpts = [m.receipt for m in posted if m.receipt_id]
        bills = [m.bill for m in posted if m.bill_id]
        entry_ids = {
            m.entry_id for m in posted if m.entry_id and not m.receipt_id and not m.bill_id
        }
        siblings = list(
            Match.all_associations.filter(entry_id__in=entry_ids, undone_at__isnull=True)
            .exclude(txn=txn)
            .select_related("txn")
        )
        for m in siblings:
            _lock(m.txn)
        now = timezone.now()
        for rcpt in rcpts:
            try:
                receipts.reverse_bank_receipt(rcpt, user=_actor(user))
            except PaymentError as e:
                raise MatchRefused(str(e)) from e
        if bills:
            from homeoa.ap import services as ap

            for bill in bills:
                try:
                    ap.reverse_payment(bill, user)
                except ap.BillError as e:
                    raise MatchRefused(str(e)) from e
        for entry in JournalEntry.all_associations.filter(pk__in=entry_ids):
            reverse_entry(entry, posted_by=_actor(user))
        for m in active + siblings:
            m.undone_at = now
            m.undone_by = _actor(user)
            m.save(update_fields=["undone_at", "undone_by"])
        for t in {txn.pk: txn, **{m.txn.pk: m.txn for m in siblings}}.values():
            _set_status(t, TxnStatus.UNMATCHED, user)
        audit.record(
            "bank.undo",
            txn,
            actor=_actor(user),
            changes={
                "entries": sorted(entry_ids),
                "receipts": sorted(r.pk for r in rcpts),
                "bills": sorted(b.pk for b in bills),
            },
        )


# ------------------------------------------------------------------ summaries


def reserve_transfer_owed(association) -> Decimal:
    """Outstanding operating -> reserve amount: the operating fund's 2900 due-to balance."""
    acct = Account.all_associations.filter(association=association, code=DUE_TO).first()
    if acct is None:
        return ZERO
    fund = receipts.billing_fund(association)
    return account_balance(acct, fund=fund) if fund else ZERO


def insurance_transfer_owed(association) -> Decimal:
    """Outstanding operating -> insurance amount: the operating fund's 2920 balance."""
    _due_from, due_to = interfund_codes("operating", "insurance")
    acct = Account.all_associations.filter(association=association, code=due_to).first()
    if acct is None:
        return ZERO
    fund = receipts.billing_fund(association)
    return account_balance(acct, fund=fund) if fund else ZERO


def active_matches(txn):
    return Match.all_associations.filter(txn=txn, undone_at__isnull=True).select_related(
        "unit", "account", "entry", "bill__vendor"
    )


def matched_total(txn) -> Decimal:
    return active_matches(txn).aggregate(s=Sum("amount"))["s"] or ZERO
