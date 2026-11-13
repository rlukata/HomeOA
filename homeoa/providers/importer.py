"""Importer: bring data in from files (generic CSV mapper by default, eCondo etc. by plugin)."""

from __future__ import annotations

import abc
import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True, kw_only=True)
class ImportPreview:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    errors: tuple[str, ...] = ()


class Importer(abc.ABC):
    #: Record kinds this importer understands, e.g. ("units", "owners").
    kinds: tuple[str, ...] = ()

    @abc.abstractmethod
    def preview(self, kind: str, data: bytes, mapping: dict[str, str]) -> ImportPreview:
        """Parse ``data`` with a column ``mapping`` (target field -> source column)."""
