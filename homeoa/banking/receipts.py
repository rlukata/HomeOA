"""Seam between deposit matching and payments: owner receipts for bank deposits.

``record_bank_receipt(txn, unit, amount, *, user=None) -> Receipt``
    ``payments.services.record_receipt`` with the bank account's GL account and fund and
    ``source="bank"``; payments allocates to open charges. A deposit into the reserve
    account gets the 2900/1900 interfund lines from ``record_receipt``.
``reverse_bank_receipt(receipt, *, user=None)``  ``payments.services.reverse_receipt``.
``linkable_receipts(txn)``  posted receipts recorded by hand (e.g. a check) for the same
    deposit account, fund and amount within ``LINK_WINDOW_DAYS`` that no deposit is
    linked to yet.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from homeoa.payments import services as payments
from homeoa.payments.models import Method, Receipt, ReceiptStatus

LINK_WINDOW_DAYS = 10


billing_fund = payments.operating_fund  # the fund owners are billed in


def _method(txn) -> str:
    text = f"{txn.payee} {txn.description}".upper()
    if "ZELLE" in text:
        return Method.ZELLE
    if "CHECK" in text or "CHK" in text:
        return Method.CHECK
    return Method.TRANSFER


def record_bank_receipt(txn, unit, amount: Decimal, *, user=None) -> Receipt:
    bank = txn.bank_account
    return payments.record_receipt(
        unit=unit,
        amount=amount,
        received_date=txn.date,
        method=_method(txn),
        deposit_account=bank.gl_account,
        fund=bank.fund,
        payer=(txn.payee or txn.description)[:200],
        reference=txn.external_id[:100],
        memo=txn.description[:200],
        posted_by=user,
        source="bank",
    )


def reverse_bank_receipt(receipt: Receipt, *, user=None) -> Receipt:
    return payments.reverse_receipt(receipt, user)


def linkable_receipts(txn):
    from .models import Match

    bank = txn.bank_account
    window = dt.timedelta(days=LINK_WINDOW_DAYS)
    linked = Match.all_associations.filter(
        association=txn.association, undone_at__isnull=True, receipt__isnull=False
    ).values("receipt_id")
    return (
        Receipt.all_associations.filter(
            association=txn.association,
            status=ReceiptStatus.POSTED,
            deposit_account=bank.gl_account,
            fund=bank.fund,
            amount=txn.amount,
            received_date__range=(txn.date - window, txn.date + window),
        )
        .exclude(pk__in=linked)
        .select_related("unit")
        .order_by("received_date", "pk")
    )
