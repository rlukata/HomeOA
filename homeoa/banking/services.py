"""Banking services: the only write path for bank transactions.

``import_file(bank_account, filename, data, *, user=None) -> ImportBatch``
    Parse an OFX/QFX/CSV upload with the default ``FileUploadFeed`` and store it. CSV uses
    the account's saved :class:`CsvColumnMapping`; with none, a mapping guessed from the
    header is saved and reused next time. Raises ``BankFileError`` (user-facing message).

``import_transactions(bank_account, transactions, *, balances=(), source, filename="",
provider="", user=None, file_sha256="") -> ImportBatch``
    Store provider ``BankTransaction`` dataclasses (feeds such as SimpleFIN call this).
    De-duplicates by ``(bank_account, external_id)``: existing ids are skipped and counted.
    Statement balances upsert by ``(bank_account, as_of)``. Nothing posts to the ledger.
    Audited as ``bank.import`` and publishes ``banking.transactions_imported``.

``set_status(txn, status)``
    Change a transaction's status (unmatched / matched / categorized / ignored). Matching
    calls this after matching or categorizing.

``undo_batch(batch, user, reason)``
    Delete a batch's transactions while all are unmatched (raises ``UndoRefused``
    otherwise); the batch stays, marked undone. Audited as ``bank.import_undo``.

``overlapping_batches(bank_account)``
    Live batch pairs from different sources with overlapping date ranges (likely
    duplicates, since CSV/OFX/feed ids differ).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.events import publish
from homeoa.providers import get_default
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.providers.bankfeed import StatementBalance as FeedBalance
from homeoa.providers.defaults.fileupload import BankFileError, guess_mapping, is_ofx, read_header

from .events import TransactionsImported
from .models import (
    BankAccount,
    BankTransaction,
    CsvColumnMapping,
    ImportBatch,
    ImportSource,
    StatementBalance,
    TxnStatus,
)

__all__ = [
    "BankFileError",
    "UndoRefused",
    "import_file",
    "import_transactions",
    "overlapping_batches",
    "set_status",
    "undo_batch",
]


def mapping_for(bank_account: BankAccount, data: bytes) -> CsvColumnMapping:
    """The saved mapping, or a new one guessed from ``data``'s header (saved)."""
    existing = CsvColumnMapping.objects.filter(bank_account=bank_account).first()
    if existing:
        return existing
    guessed = guess_mapping(read_header(data))
    if guessed is None:
        raise BankFileError(
            _("We couldn't find date and amount columns. Set up the column mapping first.")
        )
    return CsvColumnMapping.objects.create(
        association=bank_account.association,
        bank_account=bank_account,
        **CsvColumnMapping.fields_from(guessed),
    )


def import_file(bank_account: BankAccount, filename: str, data: bytes, *, user=None):
    feed = get_default("BankFeedProvider")
    ofx = is_ofx(filename, data)
    with transaction.atomic():
        mapping = None if ofx else mapping_for(bank_account, data).to_mapping()
        parsed = feed.parse_statement(filename, data, mapping=mapping)
        return import_transactions(
            bank_account,
            parsed.transactions,
            balances=parsed.balances,
            source=ImportSource.OFX if ofx else ImportSource.CSV,
            filename=filename,
            provider=type(feed).__name__,
            user=user,
            file_sha256=hashlib.sha256(data).hexdigest(),
        )


@transaction.atomic
def import_transactions(
    bank_account: BankAccount,
    transactions: Iterable[FeedTxn],
    *,
    balances: Iterable[FeedBalance] = (),
    source: str,
    filename: str = "",
    provider: str = "",
    user=None,
    file_sha256: str = "",
) -> ImportBatch:
    txns = list(transactions)
    assoc = bank_account.association
    # Serialize imports per account so concurrent uploads can't race the dedupe.
    BankAccount.all_associations.select_for_update().filter(pk=bank_account.pk).first()
    batch = ImportBatch.objects.create(
        association=assoc,
        bank_account=bank_account,
        source=source,
        filename=filename[:255],
        file_sha256=file_sha256,
        provider=provider,
        total_rows=len(txns),
        imported_by=user if getattr(user, "is_authenticated", False) else None,
    )
    ids = [t.external_id for t in txns]
    if any(not i for i in ids):
        raise ValueError("Every BankTransaction needs an external_id.")
    existing = set(
        BankTransaction.objects.filter(bank_account=bank_account, external_id__in=ids).values_list(
            "external_id", flat=True
        )
    )
    new, seen = [], set()
    for seq, t in enumerate(txns):
        if t.external_id in existing or t.external_id in seen:
            continue
        seen.add(t.external_id)
        new.append(
            BankTransaction(
                association=assoc,
                bank_account=bank_account,
                batch=batch,
                date=t.posted,
                amount=t.amount,
                description=t.description[:255],
                payee=t.payee[:255],
                memo=t.memo[:255],
                external_id=t.external_id[:255],
                running_balance=t.balance,
                sequence=seq,
            )
        )
    BankTransaction.objects.bulk_create(new)
    for b in balances:
        StatementBalance.objects.update_or_create(
            association=assoc,
            bank_account=bank_account,
            as_of=b.as_of,
            defaults={"amount": b.amount, "available": b.available, "batch": batch},
        )
    batch.imported_count = len(new)
    batch.duplicate_count = len(txns) - len(new)
    if new:
        batch.date_from = min(t.date for t in new)
        batch.date_to = max(t.date for t in new)
    batch.save()
    audit.record(
        "bank.import",
        batch,
        actor=batch.imported_by,
        changes={
            "bank_account": str(bank_account),
            "filename": filename,
            "imported": batch.imported_count,
            "duplicates": batch.duplicate_count,
        },
    )
    publish(
        TransactionsImported(
            association_id=assoc.pk,
            bank_account_id=bank_account.pk,
            batch_id=batch.pk,
            imported=batch.imported_count,
            duplicates=batch.duplicate_count,
            transaction_ids=tuple(t.pk for t in new),
        )
    )
    return batch


def set_status(txn: BankTransaction, status: str) -> BankTransaction:
    if status not in TxnStatus.values:
        raise ValueError(f"Unknown status {status!r}")
    if txn.status != status:
        old = txn.status
        txn.status = status
        txn.save(update_fields=["status"])
        audit.record("bank.status", txn, changes={"status": [old, status]})
    return txn


class UndoRefused(ValueError):
    """The batch can't be undone; ``str(err)`` is user-facing."""


def undo_batch(batch: ImportBatch, user, reason: str) -> ImportBatch:
    """Remove an import while every one of its transactions is still unmatched.

    This is the only sanctioned way to delete imported transactions (they are otherwise
    immutable). Statement balances from the batch go too. The batch row stays, marked
    undone, and the reason is audited as ``bank.import_undo``.
    """
    reason = (reason or "").strip()
    if not reason:
        raise UndoRefused(_("Give a reason for undoing this import."))
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.is_undone:
            raise UndoRefused(_("This import was already undone."))
        txns = BankTransaction.objects.filter(batch=batch)
        if txns.exclude(status=TxnStatus.UNMATCHED).exists():
            raise UndoRefused(
                _(
                    "Some transactions in this import are already matched or categorized. "
                    "Undo those first."
                )
            )
        count = txns.count()
        txns._raw_delete(txns.db)  # bypasses BankTransaction.delete(): the sanctioned path
        StatementBalance.objects.filter(batch=batch).delete()
        batch.undone_at = timezone.now()
        batch.undone_by = user if getattr(user, "is_authenticated", False) else None
        batch.undo_reason = reason[:255]
        batch.save(update_fields=["undone_at", "undone_by", "undo_reason"])
        audit.record(
            "bank.import_undo",
            batch,
            actor=batch.undone_by,
            changes={"removed": count, "reason": reason},
            message=reason,
        )
    return batch


def overlapping_batches(bank_account: BankAccount) -> list[tuple[ImportBatch, ImportBatch]]:
    """Pairs of live batches from different sources whose date ranges overlap.

    CSV, OFX and feed ids for one real transaction differ, so such overlaps usually mean
    duplicates; the board should undo one of them.
    """
    batches = list(
        ImportBatch.objects.filter(
            bank_account=bank_account, undone_at__isnull=True, date_from__isnull=False
        ).order_by("created_at", "pk")
    )
    pairs = []
    for i, a in enumerate(batches):
        for b in batches[i + 1 :]:
            if a.source != b.source and a.date_from <= b.date_to and b.date_from <= a.date_to:
                pairs.append((a, b))
    return pairs
