"""BankFeedProvider: bank transactions into HomeOA (file upload by default, feeds by plugin)."""

from __future__ import annotations

import abc
import dataclasses
import datetime
from decimal import Decimal


@dataclasses.dataclass(frozen=True, kw_only=True)
class BankTransaction:
    external_id: str
    posted: datetime.date
    amount: Decimal  # positive = money in, negative = money out
    description: str = ""
    memo: str = ""
    payee: str = ""
    #: Running balance after this transaction, when the source provides one.
    balance: Decimal | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class StatementBalance:
    """A balance the bank reported (OFX LEDGERBAL, a feed's balance) as of a date."""

    as_of: datetime.date
    amount: Decimal
    available: Decimal | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class ParsedStatement:
    transactions: list[BankTransaction]
    balances: list[StatementBalance] = dataclasses.field(default_factory=list)
    #: Bank's account number from the file (e.g. OFX ACCTID), "" when absent.
    account_number: str = ""


@dataclasses.dataclass(frozen=True, kw_only=True)
class FeedAccount:
    association_id: int
    bank_account_id: int
    external_account_id: str = ""


class BankFeedProvider(abc.ABC):
    #: True when ``fetch`` can pull transactions without a file (e.g. SimpleFIN).
    syncs: bool = False

    @abc.abstractmethod
    def parse_file(self, filename: str, data: bytes) -> list[BankTransaction]:
        """Parse an uploaded OFX/QFX/CSV statement."""

    def parse_statement(self, filename: str, data: bytes, **options) -> ParsedStatement:
        """Like ``parse_file`` but also returns statement balances. Override when known."""
        return ParsedStatement(transactions=self.parse_file(filename, data))

    def fetch(self, account: FeedAccount, since: datetime.date) -> list[BankTransaction]:
        raise NotImplementedError("This bank feed only supports file upload.")

    def sync(self, association, *, user=None):
        """Pull new transactions for every linked account (feeds with ``syncs``).

        Implementations import through ``homeoa.banking.services.import_transactions``,
        record failures for the board instead of raising, and return a result with
        ``ok``, ``imported``, ``duplicates`` and ``error``.
        """
        raise NotImplementedError("This bank feed only supports file upload.")

    def feed_status(self, association) -> dict | None:
        """For the bank page: ``name``, ``last_synced_at``, ``error``, ``manage_url``,
        ``connected``. ``None`` for upload-only feeds."""
        return None
