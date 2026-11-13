"""SignatureProvider: e-signatures (built-in click-to-sign by default)."""

from __future__ import annotations

import abc
import dataclasses
import enum


class SignatureStatus(enum.StrEnum):
    REQUESTED = "requested"
    SIGNED = "signed"
    DECLINED = "declined"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Signer:
    name: str
    email: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class SignatureRequest:
    association_id: int
    title: str
    document_key: str  # FileStorage key of the document to sign
    signers: tuple[Signer, ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class SignatureResult:
    status: SignatureStatus
    reference: str = ""
    message: str = ""


class SignatureProvider(abc.ABC):
    @abc.abstractmethod
    def request_signature(self, request: SignatureRequest) -> SignatureResult: ...
