"""Period close domain events."""

from __future__ import annotations

from dataclasses import dataclass

from homeoa.events import Event


@dataclass(frozen=True, kw_only=True)
class MonthClosed(Event):
    """A month close was recorded. Published inside the close's transaction."""

    name = "closing.month_closed"
    close_id: int
    year: int
    month: int
    closed_by_id: int | None = None
