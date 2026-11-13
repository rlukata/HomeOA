"""Default BankFeedProvider: OFX/QFX/CSV statement files uploaded by the board (FIN-07).

``FileUploadFeed().parse_statement(filename, data, mapping=None) -> ParsedStatement``
    OFX and QFX (SGML v1 or XML v2) are parsed with ``ofxtools``; transactions keep the
    bank's FITID as ``external_id`` and LEDGERBAL becomes a :class:`StatementBalance`.
    Anything else is read as CSV with a :class:`CsvMapping` (``guess_mapping`` proposes
    one from the header row when none is given).
``parse_file(filename, data, mapping=None)`` returns just the transactions.

Every transaction gets a non-empty ``external_id``. Without a bank id it is
``"h:" + sha256(date | amount | normalized description | n)`` where ``n`` counts earlier
identical (date, amount, description) rows in the same file, so re-uploads and overlapping
files reproduce the same ids while two genuine identical same-day transactions both survive.

Errors raise :class:`BankFileError` with a message fit to show the user.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext as _

from ..bankfeed import BankFeedProvider, BankTransaction, ParsedStatement, StatementBalance

CENT = Decimal("0.01")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%b %d, %Y")


class BankFileError(ValueError):
    """The file could not be read. ``str(err)`` is a user-facing message."""


class Sign:
    POSITIVE_IN = "positive_in"  # one amount column, deposits positive
    POSITIVE_OUT = "positive_out"  # one amount column, withdrawals positive (card style)
    SPLIT = "split"  # separate debit (out) and credit (in) columns


@dataclasses.dataclass(frozen=True, kw_only=True)
class CsvMapping:
    """Which CSV columns (by header name) hold which fields."""

    date: str
    description: str = ""
    amount: str = ""
    debit: str = ""
    credit: str = ""
    payee: str = ""
    memo: str = ""
    reference: str = ""
    balance: str = ""
    sign: str = Sign.POSITIVE_IN
    date_format: str = ""  # strptime format; blank tries DATE_FORMATS
    skip_rows: int = 0  # preamble lines before the header row


_GUESSES = {
    "date": ("date", "posted date", "posting date", "transaction date", "posted"),
    "description": ("description", "name", "details", "transaction", "memo"),
    "amount": ("amount", "transaction amount", "amt"),
    "debit": ("debit", "withdrawal", "withdrawals", "debit amount"),
    "credit": ("credit", "deposit", "deposits", "credit amount"),
    "payee": ("payee",),
    "reference": ("reference", "ref", "fitid", "transaction id", "id", "check number"),
    "balance": ("balance", "running balance", "running bal."),
}


def read_header(data: bytes, skip_rows: int = 0) -> list[str]:
    rows = _csv_rows(data, skip_rows)
    return [h.strip() for h in rows[0]] if rows else []


def guess_mapping(headers: list[str]) -> CsvMapping | None:
    """Propose a mapping from common header names, or None if date/amount aren't found."""
    lower = {h.strip().lower(): h for h in headers}
    found = {}
    for field, names in _GUESSES.items():
        for n in names:
            if n in lower and lower[n] not in found.values():
                found[field] = lower[n]
                break
    if "date" not in found:
        return None
    if "amount" in found:
        found.pop("debit", None)
        found.pop("credit", None)
        return CsvMapping(**found)
    if "debit" in found and "credit" in found:
        return CsvMapping(sign=Sign.SPLIT, **found)
    return None


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise BankFileError(_("The file isn't readable text."))


def _csv_rows(data: bytes, skip_rows: int) -> list[list[str]]:
    text = _decode(data)
    try:
        rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    except csv.Error as e:
        raise BankFileError(_("The CSV file is malformed: %(error)s") % {"error": e}) from e
    return rows[skip_rows:]


def parse_money(raw: str) -> Decimal | None:
    s = (raw or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").replace(" ", "")
    if s.endswith("-"):
        neg, s = True, s[:-1]
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise BankFileError(_("“%(value)s” is not an amount.") % {"value": raw}) from None
    if not value.is_finite() or value != value.quantize(CENT):
        raise BankFileError(_("“%(value)s” is not an amount in cents.") % {"value": raw})
    return (-value if neg else value).quantize(CENT)


def parse_date(raw: str, fmt: str = "") -> dt.date:
    s = (raw or "").strip()
    for f in (fmt,) if fmt else DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise BankFileError(_("“%(value)s” is not a date.") % {"value": raw})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().upper()


def assign_hash_ids(txns: list[BankTransaction]) -> list[BankTransaction]:
    """Give each transaction without an external id its stable content hash (see module doc)."""
    seen: Counter = Counter()
    out = []
    for t in txns:
        if t.external_id:
            out.append(t)
            continue
        key = (t.posted.isoformat(), f"{t.amount:.2f}", _norm(t.description))
        n = seen[key]
        seen[key] += 1
        digest = hashlib.sha256("|".join((*key, str(n))).encode()).hexdigest()[:32]
        out.append(dataclasses.replace(t, external_id=f"h:{digest}"))
    return out


class FileUploadFeed(BankFeedProvider):
    syncs = False
    max_bytes = 5 * 1024 * 1024

    def parse_file(
        self, filename: str, data: bytes, mapping: CsvMapping | None = None
    ) -> list[BankTransaction]:
        return self.parse_statement(filename, data, mapping=mapping).transactions

    def parse_statement(
        self, filename: str, data: bytes, mapping: CsvMapping | None = None, **options
    ) -> ParsedStatement:
        if not data:
            raise BankFileError(_("The file is empty."))
        if len(data) > self.max_bytes:
            raise BankFileError(_("The file is larger than 5 MB."))
        if is_ofx(filename, data):
            return self._parse_ofx(data)
        if mapping is None:
            mapping = guess_mapping(read_header(data))
            if mapping is None:
                raise BankFileError(
                    _("We couldn't find date and amount columns. Set up the column mapping.")
                )
        return self._parse_csv(data, mapping)

    # -- OFX ---------------------------------------------------------------------------

    def _parse_ofx(self, data: bytes) -> ParsedStatement:
        from ofxtools.Parser import OFXTree

        tree = OFXTree()
        try:
            tree.parse(io.BytesIO(data))
            ofx = tree.convert()
            statements = list(ofx.statements)
        except Exception as e:  # ofxtools raises a variety of parse errors
            raise BankFileError(_("This OFX/QFX file couldn't be read.")) from e
        if not statements:
            raise BankFileError(_("This OFX/QFX file has no bank statement."))
        txns, balances, number = [], [], ""
        for stmt in statements:
            number = number or str(getattr(stmt.account, "acctid", "") or "")
            for t in stmt.banktranlist or ():
                txns.append(
                    BankTransaction(
                        external_id=str(t.fitid or "").strip(),
                        posted=t.dtposted.date(),
                        amount=Decimal(t.trnamt).quantize(CENT),
                        description=(t.name or getattr(t.payee, "name", "") or "").strip(),
                        memo=(t.memo or "").strip(),
                        payee=(getattr(t.payee, "name", "") or "").strip(),
                    )
                )
            lb = getattr(stmt, "ledgerbal", None)
            if lb is not None:
                ab = getattr(stmt, "availbal", None)
                balances.append(
                    StatementBalance(
                        as_of=lb.dtasof.date(),
                        amount=Decimal(lb.balamt).quantize(CENT),
                        available=Decimal(ab.balamt).quantize(CENT) if ab is not None else None,
                    )
                )
        return ParsedStatement(
            transactions=assign_hash_ids(txns), balances=balances, account_number=number
        )

    # -- CSV ---------------------------------------------------------------------------

    def _parse_csv(self, data: bytes, m: CsvMapping) -> ParsedStatement:
        rows = _csv_rows(data, m.skip_rows)
        if not rows:
            raise BankFileError(_("The file has no rows."))
        header = [h.strip() for h in rows[0]]
        idx = {h: i for i, h in enumerate(header)}
        needed = [m.date] + ([m.debit, m.credit] if m.sign == Sign.SPLIT else [m.amount])
        optional = [m.description, m.payee, m.memo, m.reference, m.balance]
        missing = [c for c in needed if not c or c not in idx]
        missing += [c for c in optional if c and c not in idx]
        if missing:
            raise BankFileError(
                _("The file is missing the column(s): %(cols)s. Check the column mapping.")
                % {"cols": ", ".join(c or "?" for c in missing)}
            )

        def cell(row, col):
            if not col:
                return ""
            i = idx[col]
            return row[i].strip() if i < len(row) else ""

        txns = []
        for line, row in enumerate(rows[1:], start=m.skip_rows + 2):
            try:
                if m.sign == Sign.SPLIT:
                    out = parse_money(cell(row, m.debit)) or Decimal("0.00")
                    inn = parse_money(cell(row, m.credit)) or Decimal("0.00")
                    amount = abs(inn) - abs(out)
                else:
                    amount = parse_money(cell(row, m.amount))
                    if amount is None:
                        raise BankFileError(_("The amount is blank."))
                    if m.sign == Sign.POSITIVE_OUT:
                        amount = -amount
                txns.append(
                    BankTransaction(
                        external_id=cell(row, m.reference),
                        posted=parse_date(cell(row, m.date), m.date_format),
                        amount=amount,
                        description=cell(row, m.description) or cell(row, m.payee),
                        payee=cell(row, m.payee),
                        memo=cell(row, m.memo),
                        balance=parse_money(cell(row, m.balance)) if m.balance else None,
                    )
                )
            except BankFileError as e:
                raise BankFileError(
                    _("Row %(line)s: %(error)s") % {"line": line, "error": e}
                ) from e
        return ParsedStatement(transactions=assign_hash_ids(txns))


def is_ofx(filename: str, data: bytes) -> bool:
    if filename.lower().endswith((".ofx", ".qfx")):
        return True
    head = data[:512].lstrip().upper()
    return head.startswith(b"OFXHEADER") or b"<OFX>" in head
