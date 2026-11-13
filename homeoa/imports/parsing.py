"""CSV parsing for imports: entity field specs, value transforms and the mapped-row reader.

A *kind* is one entity the mapper understands (units, owners, ...). Each kind lists its target
fields; a mapping assigns a source column to each field. Transforms turn the raw text into
typed values (``Decimal`` for money, never float) and raise :class:`CellError` on bad input,
which the reader turns into an error pointing at file, row and column.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

CENT = Decimal("0.01")

DATE_FORMATS = (
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%m/%d/%y", "MM/DD/YY"),
)
DEFAULT_DATE_FORMAT = "%m/%d/%Y"


class CellError(ValueError):
    pass


# ---------------------------------------------------------------- transforms


def parse_money(raw: str) -> Decimal | None:
    """``1,234.56``, ``$1,234.56``, ``(25.00)``, ``-25``, ``25.00-`` -> Decimal; blank -> None."""
    text = (raw or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if not text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    elif text.endswith("-"):
        negative, text = True, text[:-1]
    elif text.startswith("-"):
        negative, text = True, text[1:]
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", text):
        raise CellError(_("“%(v)s” is not an amount.") % {"v": raw})
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - guarded by the regex
        raise CellError(_("“%(v)s” is not an amount.") % {"v": raw}) from None
    if value != value.quantize(CENT):
        raise CellError(_("“%(v)s” has more than two decimal places.") % {"v": raw})
    value = value.quantize(CENT)
    return -value if negative else value


def parse_decimal(raw: str) -> Decimal | None:
    text = (raw or "").strip().rstrip("%").replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise CellError(_("“%(v)s” is not a number.") % {"v": raw}) from None


def parse_date(raw: str, fmt: str = DEFAULT_DATE_FORMAT) -> dt.date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, fmt).date()
    except ValueError:
        label = dict(DATE_FORMATS).get(fmt, fmt)
        raise CellError(
            _("“%(v)s” is not a date in the format %(f)s.") % {"v": raw, "f": label}
        ) from None


def parse_bool(raw: str) -> bool | None:
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in {"yes", "y", "true", "1", "x"}:
        return True
    if text in {"no", "n", "false", "0"}:
        return False
    raise CellError(_("“%(v)s” is not Yes or No.") % {"v": raw})


def parse_text(raw: str) -> str:
    return (raw or "").strip()


def split_address(raw: str) -> dict[str, str]:
    """``"14207 Cedar Row Ln #7, Olympia, WA 98501"`` -> address_line1/city/region/postal_code."""
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    out = {"address_line1": "", "city": "", "region": "", "postal_code": ""}
    if not parts:
        return out
    m = re.fullmatch(r"([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)", parts[-1]) if len(parts) >= 3 else None
    if m:
        out.update(
            address_line1=", ".join(parts[:-2]),
            city=parts[-2],
            region=m.group(1).upper(),
            postal_code=m.group(2),
        )
    else:
        out["address_line1"] = ", ".join(parts)
    return out


def join_name(first: str, last: str) -> str:
    return " ".join(p for p in (first.strip(), last.strip()) if p)


# ---------------------------------------------------------------- kinds


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"  # text | money | decimal | date | bool
    required: bool = False
    # Header names this field is guessed from (lower-case), besides its label.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    fields: tuple[Field, ...]
    # Fields where one of the group must be mapped, e.g. full name or first+last.
    one_of: tuple[tuple[str, ...], ...] = ()

    def field(self, name: str) -> Field:
        return next(f for f in self.fields if f.name == name)


L = gettext_lazy
KINDS: dict[str, Kind] = {
    k.name: k
    for k in (
        Kind(
            "units",
            L("Units"),
            (
                Field("number", L("Unit number"), required=True, aliases=("unit", "unit #")),
                Field("building", L("Building"), aliases=("bldg",)),
                Field("address", L("Address")),
                Field("parcel_id", L("Parcel ID"), aliases=("parcel", "apn")),
                Field(
                    "allocated_interest",
                    L("Allocated interest %"),
                    "decimal",
                    aliases=("allocated interest %", "percentage", "share"),
                ),
                Field("status", L("Status")),
            ),
        ),
        Kind(
            "owners",
            L("Owners"),
            (
                Field("unit", L("Unit number"), required=True, aliases=("unit #",)),
                Field("name", L("Full name"), aliases=("owner name", "owner", "name")),
                Field("first_name", L("First name")),
                Field("last_name", L("Last name")),
                Field("email", L("Email"), aliases=("primary email", "e-mail")),
                Field("phone", L("Phone")),
                Field("mailing_address", L("Mailing address")),
                Field("start", L("Owned since"), "date", aliases=("move in", "start date")),
                Field("end", L("Owned until"), "date", aliases=("move out", "end date")),
                Field("e_delivery", L("E-delivery consent"), "bool", aliases=("e-delivery",)),
            ),
            one_of=(("name", "last_name"),),
        ),
        Kind(
            "tenants",
            L("Tenants"),
            (
                Field("unit", L("Unit number"), required=True, aliases=("unit #",)),
                Field("name", L("Full name"), aliases=("tenant name", "tenant", "name")),
                Field("first_name", L("First name")),
                Field("last_name", L("Last name")),
                Field("email", L("Email")),
                Field("phone", L("Phone")),
                Field("start", L("Lease start"), "date", required=True),
                Field("end", L("Lease end"), "date"),
            ),
            one_of=(("name", "last_name"),),
        ),
        Kind(
            "history",
            L("Charges and payments history"),
            (
                Field("unit", L("Unit number"), required=True, aliases=("unit #",)),
                Field("date", L("Date"), "date", required=True),
                Field("type", L("Type"), required=True),
                Field("description", L("Description"), aliases=("memo",)),
                Field("charge", L("Charge"), "money", aliases=("charges", "debit")),
                Field("payment", L("Payment"), "money", aliases=("payments", "credit")),
                Field("balance", L("Running balance"), "money", aliases=("balance",)),
                Field("reference", L("Reference"), aliases=("ref", "check #")),
            ),
        ),
        Kind(
            "balances",
            L("Opening owner balances"),
            (
                Field("unit", L("Unit number"), required=True, aliases=("unit #",)),
                Field("name", L("Owner name"), aliases=("owner name",)),
                Field("balance", L("Balance"), "money", required=True),
                Field("prepaid", L("Prepaid"), "money"),
            ),
        ),
        Kind(
            "accounts",
            L("Chart of accounts"),
            (
                Field("code", L("Account number"), required=True, aliases=("account number",)),
                Field("name", L("Account name"), required=True, aliases=("account name",)),
                Field("type", L("Account type"), required=True, aliases=("account type",)),
                Field("fund", L("Fund")),
            ),
        ),
        Kind(
            "trial_balance",
            L("Trial balance"),
            (
                Field("code", L("Account number"), required=True, aliases=("account number",)),
                Field("name", L("Account name"), aliases=("account name",)),
                Field("debit", L("Debit"), "money"),
                Field("credit", L("Credit"), "money"),
            ),
        ),
    )
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9%#]+", " ", str(text).lower()).strip()


def guess_columns(kind: Kind, headers: list[str]) -> dict[str, str]:
    """Best-effort default mapping from a header row: label or alias match."""
    by_norm = {_norm(h): h for h in headers}
    out = {}
    for f in kind.fields:
        for cand in (f.name.replace("_", " "), str(f.label), *f.aliases):
            if _norm(cand) in by_norm:
                out[f.name] = by_norm[_norm(cand)]
                break
    return out


def read_csv(content: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = headers
    rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
    return headers, rows


@dataclass
class Problem:
    file: str
    row: int | None
    column: str
    message: str

    def as_dict(self) -> dict:
        return {"file": self.file, "row": self.row, "column": self.column, "message": self.message}


@dataclass
class Parsed:
    kind: str
    file: str
    rows: list[dict] = field(default_factory=list)  # each has "_row": spreadsheet row number


_PARSERS = {"money": parse_money, "decimal": parse_decimal, "bool": parse_bool}


def parse_file(
    kind_name: str,
    filename: str,
    content: str,
    columns: dict[str, str],
    date_format: str,
    problems: list[Problem],
) -> Parsed:
    """Read ``content`` with ``columns`` (field -> header). Bad cells go to ``problems``."""
    kind = KINDS[kind_name]
    headers, raw_rows = read_csv(content)
    out = Parsed(kind_name, filename)
    columns = {k: v for k, v in columns.items() if v}
    ok = True
    for f in kind.fields:
        col = columns.get(f.name)
        if col and col not in headers:
            problems.append(
                Problem(filename, None, col, _("The file has no column “%(c)s”.") % {"c": col})
            )
            ok = False
        elif f.required and not col:
            problems.append(
                Problem(filename, None, "", _("Choose a column for “%(f)s”.") % {"f": str(f.label)})
            )
            ok = False
    for group in kind.one_of:
        if not any(columns.get(g) for g in group):
            labels = " / ".join(str(kind.field(g).label) for g in group)
            problems.append(
                Problem(filename, None, "", _("Choose a column for “%(f)s”.") % {"f": labels})
            )
            ok = False
    if not ok:
        return out
    for i, raw in enumerate(raw_rows, start=2):
        if not any(v.strip() for v in raw.values()):
            continue
        row: dict = {"_row": i}
        for f in kind.fields:
            col = columns.get(f.name)
            text = raw.get(col, "") if col else ""
            try:
                if f.type == "date":
                    value = parse_date(text, date_format)
                elif f.type in _PARSERS:
                    value = _PARSERS[f.type](text)
                else:
                    value = parse_text(text)
                if f.required and value in (None, ""):
                    raise CellError(_("This value is required."))
            except CellError as e:
                problems.append(Problem(filename, i, col or str(f.label), str(e)))
                value = None
            row[f.name] = value
        if "first_name" in row and not row.get("name"):
            row["name"] = join_name(row.get("first_name") or "", row.get("last_name") or "")
            if not row["name"] and kind.one_of:
                problems.append(Problem(filename, i, "", _("The name is missing.")))
        out.rows.append(row)
    return out
