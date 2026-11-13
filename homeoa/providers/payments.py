"""PaymentProvider: collecting owner payments (inbound only; HomeOA never pays out)."""

from __future__ import annotations

import abc
import dataclasses
import enum
from decimal import Decimal


class PaymentStatus(enum.StrEnum):
    MANUAL = "manual"  # nothing happens online; the board records the payment by hand
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True, kw_only=True)
class PaymentRequest:
    association_id: int
    unit_id: int
    amount: Decimal
    description: str = ""
    payer_email: str = ""
    idempotency_key: str = ""
    #: The provider's reference for the stored debit authorization (``SavedMethod.ref``).
    mandate_ref: str = ""
    #: The ownership period the payment applies to (default: the current one).
    ownership_period_id: int | None = None


@dataclasses.dataclass(frozen=True, kw_only=True)
class PaymentResult:
    status: PaymentStatus
    reference: str = ""
    fee: Decimal = Decimal("0.00")
    message: str = ""


@dataclasses.dataclass(frozen=True, kw_only=True)
class SavedMethod:
    """A bank account the owner authorized for debits (a stored mandate)."""

    ref: str
    label: str
    #: False while the bank account still waits for verification (e.g. microdeposits).
    chargeable: bool = True


@dataclasses.dataclass(frozen=True, kw_only=True)
class PendingPayment:
    """An online payment that was started but has not cleared yet."""

    reference: str
    amount: Decimal
    created: object = None


class PaymentProvider(abc.ABC):
    """Collects an owner payment. Posting to the ledger happens in ``payments`` services."""

    #: True when the provider can take money online (pay-now / autopay).
    online: bool = False

    @abc.abstractmethod
    def collect(self, request: PaymentRequest) -> PaymentResult: ...

    # Optional, for online providers (pay now / autopay). Defaults: nothing stored.

    def saved_methods(self, unit, person) -> list[SavedMethod]:
        """The person's authorized bank accounts for ``unit`` (newest first)."""
        return []

    def setup_url(self, unit) -> str:
        """Where the owner links and verifies a bank account ("" when not supported)."""
        return ""

    def pending(self, unit, ownership_period) -> list[PendingPayment]:
        """Payments started for ``ownership_period`` that have not cleared yet."""
        return []

    def fee_quote(self, association, amount: Decimal) -> Decimal:
        """The fee the owner pays on top of ``amount`` (0 when the association absorbs it)."""
        return Decimal("0.00")
