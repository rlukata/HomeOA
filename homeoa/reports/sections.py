"""What a board packet section receives and returns.

Sections are contributed to the ``BOARD_PACKET_SECTIONS`` slot as callables taking a
``PacketContext`` and returning a ``PacketPart``. A part may add rows to the packet's
summary table, which comes first, and plain sentences to the paragraph above it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from .export import TableSpec


@dataclasses.dataclass(frozen=True)
class PacketContext:
    association: object
    start: dt.date  # first day of the month
    end: dt.date  # last day of the month
    fy_start: dt.date  # first day of the fiscal year holding ``end``


@dataclasses.dataclass(frozen=True)
class PacketPart:
    tables: list[TableSpec] = dataclasses.field(default_factory=list)
    summary_rows: list[list] = dataclasses.field(default_factory=list)
    sentences: list[str] = dataclasses.field(default_factory=list)
