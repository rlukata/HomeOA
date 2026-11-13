"""``Importer`` implementations backed by the import engine's parser."""

from __future__ import annotations

from homeoa.providers.importer import Importer, ImportPreview

from .parsing import DEFAULT_DATE_FORMAT, KINDS, Problem, parse_file, read_csv
from .profiles import APPFOLIO_PROFILE


class MappedCsvImporter(Importer):
    """Generic CSV mapper: any kind, columns chosen by the caller."""

    kinds = tuple(KINDS)
    default_columns: dict = {}

    def preview(self, kind: str, data: bytes, mapping: dict[str, str]) -> ImportPreview:
        content = data.decode("utf-8-sig")
        headers, _rows = read_csv(content)
        problems: list[Problem] = []
        columns = mapping or self.default_columns.get(kind, {})
        parsed = parse_file(kind, kind, content, columns, DEFAULT_DATE_FORMAT, problems)
        errors = []
        for p in problems:
            where = " ".join(s for s in (f"row {p.row}" if p.row else "", p.column) if s)
            errors.append(f"{where}: {p.message}" if where else p.message)
        return ImportPreview(
            columns=tuple(headers),
            rows=tuple({k: r[k] for k in columns if k in r} for r in parsed.rows),
            errors=tuple(errors),
        )


class AppFolioImporter(MappedCsvImporter):
    """AppFolio-style exports with the default AppFolio column profile."""

    default_columns = APPFOLIO_PROFILE["columns"]
