"""Default EmailChannel: SMTP through Django's mail backend (``EMAIL_*`` settings).

Provider message id: some SMTP relays (e.g. Resend) replace our ``Message-ID``
and identify the message in bounce webhooks only by their own id, which they return
in the final DATA reply (``250 <id>``). When Django's SMTP backend is in use we keep
that reply and extract the id with :func:`reply_id`; the result then carries it as
``provider_id`` next to our own ``message_id``. Provider-neutral: no relay-specific
code, and with no id in the reply the result is exactly as before.
"""

import contextlib
import logging
import re
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.mail.backends.smtp import EmailBackend as DjangoSmtpBackend
from django.core.mail.message import make_msgid

from ..email import EmailChannel, EmailRequest, EmailResult, classify_send_error

logger = logging.getLogger(__name__)

DJANGO_SMTP = "django.core.mail.backends.smtp.EmailBackend"

_ENHANCED_STATUS = re.compile(r"^[245]\.\d{1,3}\.\d{1,3}\s+")
_BRACKETED = re.compile(r"<([^<>\s]{1,200})>")
_BARE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+=/-]{7,199}$")


def reply_id(reply: bytes | str) -> str:
    """The relay's message id from a DATA reply text (after the ``250`` code), or "".

    Rule: an optional enhanced status code (``2.0.0``) is dropped; then
    1. a single ``<token>`` anywhere in the reply is the id (``<`` ``>`` stripped);
    2. otherwise a reply that is one bare token of at least 8 id characters with at
       least one digit is the id (``250 abc12345``);
    3. anything else (``OK``, ``Ok: queued as X``, free text) yields no id.
    """
    if isinstance(reply, bytes):
        reply = reply.decode("utf-8", "replace")
    text = _ENHANCED_STATUS.sub("", reply.strip())
    bracketed = _BRACKETED.findall(text)
    if len(bracketed) == 1:
        return bracketed[0]
    if bracketed:
        return ""
    if _BARE_ID.match(text) and any(c.isdigit() for c in text):
        return text
    return ""


class ReplyCapturingSmtpBackend(DjangoSmtpBackend):
    """Django's SMTP backend that remembers the last DATA reply text.

    ``sendmail`` (SSL or STARTTLS alike) ends with ``self.data(msg)``; we wrap that call
    on the open connection, so errors and their classification are unchanged.
    """

    last_reply: bytes = b""

    def _send(self, email_message):
        conn = self.connection
        original = conn.data

        def data(msg):
            code, resp = original(msg)
            self.last_reply = resp or b""
            return code, resp

        conn.data = data
        try:
            return super()._send(email_message)
        finally:
            with contextlib.suppress(AttributeError):
                del conn.data


class SmtpEmail(EmailChannel):
    def _connection(self):
        if settings.EMAIL_BACKEND == DJANGO_SMTP:
            return get_connection(f"{__name__}.ReplyCapturingSmtpBackend")
        return get_connection()

    def send(self, message: EmailRequest) -> EmailResult:
        msg_id = make_msgid(domain="homeoa")
        connection = self._connection()
        mail = EmailMultiAlternatives(
            subject=message.subject,
            body=message.text,
            from_email=message.from_email or None,
            to=list(message.to),
            reply_to=list(message.reply_to) or None,
            headers={"Message-ID": msg_id, **message.headers},
            connection=connection,
        )
        if message.html:
            mail.attach_alternative(message.html, "text/html")
        for att in message.attachments:
            mail.attach(att.filename, att.content, att.content_type)
        try:
            mail.send()
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("Email send failed: %s", exc)
            return EmailResult(sent=False, error=str(exc), failure_kind=classify_send_error(exc))
        provider_id = reply_id(getattr(connection, "last_reply", b""))
        return EmailResult(sent=True, message_id=msg_id, provider_id=provider_id)
