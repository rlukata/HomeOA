"""Banking domain events."""

from __future__ import annotations

from dataclasses import dataclass

from homeoa.events import Event


@dataclass(frozen=True, kw_only=True)
class TransactionsImported(Event):
    """New bank transactions were stored (file upload or feed sync). Matching picks them up."""

    name = "banking.transactions_imported"
    bank_account_id: int
    batch_id: int
    imported: int
    duplicates: int
    transaction_ids: tuple[int, ...] = ()
