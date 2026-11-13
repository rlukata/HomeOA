"""Retention guard: records that were sent or posted can never be deleted by code.

Financial records, contracts and copies of every notice sent are kept at least 7 years and
ballots 1 year. Only unsent drafts and temporary files may be deleted. The guard hangs on
``pre_delete``, which Django sends for ``instance.delete()``, ``queryset.delete()`` and every
cascaded row alike (a receiver also turns off fast deletes), so no ORM delete path skips it.
"""

from django.db.models import Q
from django.db.models.signals import pre_delete


class RetentionError(Exception):
    """Raised when code tries to delete a record that must be kept."""


_SENT_DELIVERY = ("sent", "delivered", "failed", "bounced")


def statement_block(statement) -> str | None:
    if statement.status == "sent":
        return "statement was sent"
    return None


def letter_block(letter) -> str | None:
    from homeoa.letters.models import MailBatch
    from homeoa.statements.models import DelinquencyAction

    items = letter.mail_items.all()
    if items.exclude(batch__status=MailBatch.Status.DRAFT).exists():
        return "letter is in a mail batch that went out"
    if items.filter(deliveries__status__in=_SENT_DELIVERY).exists():
        return "letter has a sent delivery"
    if DelinquencyAction.all_associations.filter(letter=letter).exists():
        return "letter belongs to a delinquency action"
    return None


def batch_block(batch) -> str | None:
    if batch.status != "draft":
        return "mail batch is not a draft"
    return None


def notification_block(note) -> str | None:
    if note.deliveries.exclude(status="preview").exists():
        return "notification has a non-preview delivery"
    return None


def delivery_block(delivery) -> str | None:
    if delivery.status != "preview":
        return "delivery is not a preview"
    return None


GUARDS = {
    "statements.Statement": statement_block,
    "letters.Letter": letter_block,
    "letters.MailBatch": batch_block,
    "notify.Notification": notification_block,
    "notify.Delivery": delivery_block,
}

_receivers = []  # signals hold weak references


def _make_receiver(label, check):
    def receiver(sender, instance, **kwargs):
        reason = check(instance)
        if reason:
            raise RetentionError(f"{label} {instance.pk} must be kept: {reason}")

    return receiver


def connect() -> None:
    for label, check in GUARDS.items():
        receiver = _make_receiver(label, check)
        _receivers.append(receiver)
        pre_delete.connect(receiver, sender=label, dispatch_uid=f"retention:{label}")


def assert_deletable_files(association, keys) -> None:
    """Raise if a stored-file key is still referenced by a record that is kept."""
    from homeoa.letters.models import Letter, MailBatch
    from homeoa.notify.models import Notification

    keys = [k for k in keys if k]
    if not keys:
        return
    if Letter.all_associations.filter(association=association, file_key__in=keys).exists():
        raise RetentionError("A stored letter file that is still in use cannot be deleted.")
    sent_batches = MailBatch.all_associations.filter(association=association).exclude(
        status=MailBatch.Status.DRAFT
    )
    if sent_batches.filter(Q(pdf_key__in=keys) | Q(manifest_key__in=keys)).exists():
        raise RetentionError("A sent mail batch's files cannot be deleted.")
    notes = (
        Notification.all_associations.filter(association=association)
        .exclude(attachments=[])
        .filter(deliveries__in=_non_preview())
        .distinct()
    )
    for note in notes:
        if any(a.get("file_key") in keys for a in note.attachments or []):
            raise RetentionError("A file attached to a sent notice cannot be deleted.")


def _non_preview():
    from homeoa.notify.models import Delivery

    return Delivery.all_associations.exclude(status=Delivery.Status.PREVIEW)
