"""Email channel (COM-02): sends through the ``EmailChannel`` provider as a background job.

``EmailChannel.send(delivery)`` only defers ``notify.send_email``; the delivery stays
``queued`` until the job runs. The job renders plain text and HTML from
``notify/email/message.txt`` / ``.html``, hands the message to
``providers.get_provider("EmailChannel", association)`` (SMTP by default; the locmem
backend in tests, the dev mail catcher in development) and moves the delivery to ``sent``
(provider id = Message-ID) or ``failed``. A bounce later moves it to ``bounced``
(see ``homeoa.notify.bounces``).

Attachments (``Notification.attachments``) are read from FileStorage at send time. A
missing or oversized file fails the delivery with ``failure_kind="attachment"``: the board
gets the failed-delivery to-do, but it does not count toward revoking e-notice consent
, since the owner's address did not fail. Likewise a send that fails before the server
accepts it (auth error, outage, timeout: ``failure_kind="transport"``) does not count;
only bounces and SMTP recipient refusals (``"recipient"``) do.

The job re-checks the resident gate before sending: while residents are off, a
delivery to anyone not on the board is turned into a preview and never sent.
"""

from __future__ import annotations

import logging
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import EMAIL, Channel, ChannelError

logger = logging.getLogger(__name__)

# Most providers reject messages over ~10-25 MB; keep attachments well under that.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


class AttachmentError(ChannelError):
    """A file to attach is missing or too large (not a failure of the channel)."""


class EmailChannel(Channel):
    key = EMAIL
    label = _("Email")
    deferred = True

    def send(self, delivery) -> str:
        from homeoa.notify.tasks import send_email

        send_email.defer(delivery_id=delivery.pk)
        return ""


def address_for(person) -> str:
    from homeoa.notify.services import current_consent

    pref = current_consent(person)
    return (pref.email if pref and pref.valid_consent else "") or person.email or ""


def sender_for(association) -> str:
    """``"<association name> <DEFAULT_FROM_EMAIL address>"``."""
    _, addr = parseaddr(settings.DEFAULT_FROM_EMAIL)
    return formataddr((association.name, addr or settings.DEFAULT_FROM_EMAIL))


def build_request(delivery):
    from homeoa.providers.email import EmailRequest

    note = delivery.notification
    address = address_for(note.recipient)
    if not address:
        raise ChannelError("no email address on file")
    context = {
        "association": note.association,
        "notification": note,
        "recipient": note.recipient,
        "subject": note.subject,
        "body": note.body,
    }
    return EmailRequest(
        to=(address,),
        from_email=sender_for(note.association),
        reply_to=tuple(a for a in [settings.HOMEOA_EMAIL_REPLY_TO] if a),
        attachments=load_attachments(note),
        subject=note.subject,
        text=render_to_string("notify/email/message.txt", context).strip() + "\n",
        html=render_to_string("notify/email/message.html", context),
        headers={"X-HomeOA-Delivery": str(delivery.pk)},
    )


def load_attachments(note) -> tuple:
    from homeoa import providers
    from homeoa.providers.email import Attachment
    from homeoa.providers.storage import FileNotFound

    specs = note.attachments or []
    if not specs:
        return ()
    storage = providers.get_provider("FileStorage", note.association)
    out, total = [], 0
    for spec in specs:
        name = spec.get("name") or "attachment"
        try:
            content = storage.get(spec["file_key"])
        except (FileNotFound, KeyError) as exc:
            raise AttachmentError(f"attachment {name} is missing") from exc
        total += len(content)
        if total > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(f"attachments exceed {MAX_ATTACHMENT_BYTES} bytes")
        out.append(
            Attachment(
                filename=name,
                content=content,
                content_type=spec.get("content_type") or "application/octet-stream",
            )
        )
    return tuple(out)


def deliver(delivery) -> None:
    """Send one queued email delivery now (the job body). Send errors fail the delivery."""
    from homeoa import providers
    from homeoa.core import residents_enabled
    from homeoa.notify.consent import ATTACHMENT_FAILURE, TRANSPORT_FAILURE
    from homeoa.notify.models import Delivery
    from homeoa.notify.services import is_board
    from homeoa.providers.email import classify_send_error

    if delivery.status != Delivery.Status.QUEUED or delivery.channel != EMAIL:
        return
    note = delivery.notification
    association = note.association
    now = timezone.now()
    if not residents_enabled(association) and not is_board(note.recipient, association):
        delivery.status = Delivery.Status.PREVIEW
        delivery.save(update_fields=["status"])
        return
    request = None
    failure_kind = ""
    try:
        request = build_request(delivery)
        result = providers.get_provider("EmailChannel", association).send(request)
        error = result.error
        sent = result.sent
        failure_kind = "" if sent else (result.failure_kind or TRANSPORT_FAILURE)
    except AttachmentError as exc:
        logger.warning("email delivery %s failed: %s", delivery.pk, exc)
        error, sent, result = str(exc), False, None
        failure_kind = ATTACHMENT_FAILURE
    except Exception as exc:  # provider or template errors fail this delivery only
        logger.warning("email delivery %s failed: %s", delivery.pk, exc)
        error, sent, result = str(exc), False, None
        failure_kind = classify_send_error(exc)
    if sent:
        delivery.status = Delivery.Status.SENT
        delivery.sent_at = now
        # The relay's id when it reported one, else our Message-ID.
        delivery.provider_id = (result.provider_id or result.message_id)[:200]
        delivery.message_id = result.message_id[:200]
        delivery.address = request.to[0]
        delivery.error = ""
    else:
        delivery.status = Delivery.Status.FAILED
        delivery.failed_at = now
        delivery.error = (error or "send failed")[:2000]
        delivery.failure_kind = failure_kind
        if request is not None:
            delivery.address = request.to[0]
    delivery.save()
    if not sent:
        from homeoa.notify.consent import after_email_outcome

        after_email_outcome(delivery)
