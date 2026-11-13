"""Ledger domain events."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from homeoa.events import Event


@dataclass(frozen=True, kw_only=True)
class EntryPosted(Event):
    """A journal entry was posted (including reversals)."""

    name = "ledger.entry_posted"
    entry_id: int
    entry_date: dt.date
    source_type: str
    source_id: str
    total: Decimal
    reversal_of_id: int | None = None
