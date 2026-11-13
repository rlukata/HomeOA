"""EmailChannel: outbound email (SMTP via Django mail by default)."""

from __future__ import annotations

import abc
import dataclasses


@dataclasses.dataclass(frozen=True, kw_only=True)
class Attachment:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclasses.dataclass(frozen=True, kw_only=True)
class EmailRequest:
    to: tuple[str, ...]
    subject: str
    text: str
    html: str = ""
    from_email: str = ""  # empty = DEFAULT_FROM_EMAIL
    reply_to: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    headers: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, kw_only=True)
class EmailResult:
    sent: bool
    message_id: str = ""  # our own Message-ID header
    # The relay's own id for the message when it reports one (e.g. Resend's DATA reply
    # "250 <id>"); bounce webhooks may only know this one. Empty = none reported.
    provider_id: str = ""
    error: str = ""
    # Why a failed send failed: RECIPIENT (the address was refused) or TRANSPORT
    # (auth error, outage, timeout...). Only recipient failures count toward consent revocation.
    failure_kind: str = ""


RECIPIENT = "recipient"
TRANSPORT = "transport"


def classify_send_error(exc: BaseException) -> str:
    """RECIPIENT for an SMTP refusal of every recipient, else TRANSPORT."""
    import smtplib

    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return RECIPIENT
    return TRANSPORT


class EmailChannel(abc.ABC):
    @abc.abstractmethod
    def send(self, message: EmailRequest) -> EmailResult: ...
