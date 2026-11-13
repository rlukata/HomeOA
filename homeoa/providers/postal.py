"""PostalProvider: physical letters (print-ready PDF batch by default, Lob by plugin)."""

from __future__ import annotations

import abc
import dataclasses
import enum


class LetterStatus(enum.StrEnum):
    QUEUED_FOR_PRINT = "queued_for_print"  # someone prints and mails it
    SUBMITTED = "submitted"  # handed to a mail service
    FAILED = "failed"


@dataclasses.dataclass(frozen=True, kw_only=True)
class PostalAddress:
    name: str
    line1: str
    city: str
    region: str
    postal_code: str
    line2: str = ""
    country: str = "US"


@dataclasses.dataclass(frozen=True, kw_only=True)
class LetterRequest:
    association_id: int
    to: PostalAddress
    pdf: bytes
    certified: bool = False
    description: str = ""


@dataclasses.dataclass(frozen=True, kw_only=True)
class LetterResult:
    status: LetterStatus
    reference: str = ""
    tracking: str = ""
    message: str = ""


class PostalProvider(abc.ABC):
    #: True when letters are printed and mailed by hand (the board confirms the mailing);
    #: False when ``send_letter`` hands them to a mail service.
    prints_locally: bool = False
    name: str = ""

    @abc.abstractmethod
    def send_letter(self, letter: LetterRequest) -> LetterResult: ...
