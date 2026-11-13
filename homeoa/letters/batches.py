"""Postal mail batches (COM-04): turn generated letters into a batch and mail it.

Flow
----
1. ``create_batch(association, letters)`` makes a draft batch. Each letter is filed through
   ``notify("letters.mailed", owners, unit=letter.unit, obj=letter)`` so it shows in the
   unit's comm log with a ``postal`` delivery, and the combined print PDF + CSV manifest
   are built (``providers.defaults.postal.build_print_batch``), whatever the provider.
2. ``send_batch(batch)`` hands each letter to the association's ``PostalProvider``:

   * default (print batch): letters are queued for printing, the batch is "ready"; the
     board prints, mails by hand and calls ``confirm_mailed``, which marks the items
     "printed (mailed by hand)" and the deliveries "sent".
   * a mail service (Lob plugin): each letter is submitted, the provider id and tracking
     number are stored and deliveries become "sent"; tracking webhooks call
     ``record_tracking_event`` to move them to delivered or returned.

3. Resident gate: while residents are off, a batch stays a draft. Sending and
   confirming raise ``ResidentsOff``; the board can still download the print file to
   check it.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, providers
from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.notify import types as ntypes
from homeoa.notify.models import Delivery
from homeoa.providers.defaults.postal import build_print_batch
from homeoa.providers.postal import LetterRequest, LetterStatus, PostalAddress

from . import merge, services
from .models import Letter, MailBatch, MailItem

NOTIFICATION_TYPE = "letters.mailed"
POSTAL = "postal"


class BatchError(Exception):
    pass


class ResidentsOff(BatchError):
    """Nothing goes to residents while the switch is off; the batch stays a draft."""


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=NOTIFICATION_TYPE,
            label=gettext_lazy("Letter by mail"),
            default_channels=(POSTAL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            mandatory=True,
            subject_template="{{ subject }}",
            body_template="{{ subject }}",
        )
    )


# --- building ---------------------------------------------------------------------------


def postal_address(letter: Letter) -> PostalAddress:
    """The structured mailing address the letter was printed with."""
    ctx = services.context_for(letter.association, letter.unit, letter.as_of)
    person = merge._primary_owner(ctx)
    source = person if person is not None and person.address_lines else letter.unit
    return PostalAddress(
        name=letter.recipient_name,
        line1=source.address_line1,
        line2=source.address_line2,
        city=source.city,
        region=source.region,
        postal_code=source.postal_code,
        country=source.country or "US",
    )


def letter_request(item: MailItem) -> LetterRequest:
    letter = item.letter
    return LetterRequest(
        association_id=letter.association_id,
        to=postal_address(letter),
        pdf=services.letter_bytes(letter),
        certified=item.batch.certified,
        description=f"{letter.unit.number} · {letter.subject}"[:200],
    )


def _items(batch: MailBatch):
    return batch.items.select_related("letter__unit", "letter__association", "batch").order_by(
        "letter__unit__number", "pk"
    )


def build_print_files(batch: MailBatch) -> MailBatch:
    """(Re)build the combined PDF and the CSV manifest and store them."""
    printed = build_print_batch([letter_request(i) for i in _items(batch)])
    storage = providers.get_provider("FileStorage", batch.association)
    pdf = storage.put(
        batch.association_id,
        f"mail-batch-{batch.pk}.pdf",
        printed.pdf,
        content_type="application/pdf",
        folder="letters/batches",
    )
    manifest = storage.put(
        batch.association_id,
        f"mail-batch-{batch.pk}.csv",
        printed.manifest_csv.encode(),
        content_type="text/csv",
        folder="letters/batches",
    )
    batch.pdf_key, batch.manifest_key, batch.pages = pdf.key, manifest.key, printed.pages
    batch.save(update_fields=["pdf_key", "manifest_key", "pages"])
    return batch


SENT_BATCH = (MailBatch.Status.READY, MailBatch.Status.MAILED, MailBatch.Status.SUBMITTED)
RETRYABLE = (MailItem.Status.FAILED, MailItem.Status.RETURNED)


def active_items(letters, exclude_batch=None):
    """Items that already mail these letters: in a sent batch and not failed or returned."""
    qs = MailItem.all_associations.filter(letter__in=letters, batch__status__in=SENT_BATCH).exclude(
        status__in=RETRYABLE
    )
    if exclude_batch is not None:
        qs = qs.exclude(batch=exclude_batch)
    return qs


def _check_not_mailed(letters, exclude_batch=None) -> None:
    taken = sorted(
        {
            i.letter.unit.number
            for i in active_items(letters, exclude_batch).select_related("letter__unit")
        }
    )
    if taken:
        raise BatchError(
            _("Already mailed in another batch (units %(units)s).") % {"units": ", ".join(taken)}
        )


def _link_postal(item: MailItem):
    def link(notes) -> None:
        item.deliveries.set(
            Delivery.all_associations.filter(notification__in=notes, channel=POSTAL)
        )

    return link


@transaction.atomic
def create_batch(
    association, letters, *, name="", certified=False, user=None, preview_only=False
) -> MailBatch:
    from homeoa.notify.services import notify_after_commit
    from homeoa.registry.services import current_owners

    letters = list(dict.fromkeys(letters))
    if not letters:
        raise BatchError(_("Choose at least one letter."))
    if any(letter.association_id != association.pk for letter in letters):
        raise BatchError("letters from another association")
    _check_not_mailed(letters)
    actor = user if user is not None and user.is_authenticated else None
    batch = MailBatch.objects.create(
        association=association,
        name=name
        or _("Letters of %(date)s") % {"date": association_today(association).isoformat()},
        certified=certified,
        created_by=actor,
    )
    for letter in letters:
        item = MailItem.objects.create(association=association, batch=batch, letter=letter)
        owners = current_owners(letter.unit, letter.as_of)
        # The mailed notice (and its postal delivery link) is made after commit;
        # a failure is logged and audited, never raised.
        notify_after_commit(
            NOTIFICATION_TYPE,
            owners,
            {"subject": letter.subject or str(letter.template_version.template)},
            letter.as_of,
            obj=letter,
            period=f"batch:{batch.pk}",
            association=association,
            unit=letter.unit,
            preview_only=preview_only,
            on_sent=_link_postal(item),
        )
    build_print_files(batch)
    audit.record(
        "mail_batch.create",
        batch,
        changes={"letters": [letter.pk for letter in letters], "certified": certified},
        actor=user,
    )
    return batch


# --- sending ----------------------------------------------------------------------------


def _check_gate(batch: MailBatch) -> None:
    if not residents_enabled(batch.association):
        raise ResidentsOff(
            _("Residents are not switched on yet, so this batch stays a draft. Nothing is sent.")
        )


def _mark_deliveries(item: MailItem, status: str, *, provider_id="", error="") -> None:
    now = timezone.now()
    for d in item.deliveries.exclude(
        status__in=[Delivery.Status.DELIVERED, Delivery.Status.BOUNCED]
    ):
        if status == Delivery.Status.SENT and d.status not in (
            Delivery.Status.QUEUED,
            Delivery.Status.PREVIEW,
            Delivery.Status.FAILED,
        ):
            continue
        d.status = status
        if provider_id:
            d.provider_id = provider_id[:200]
        if status == Delivery.Status.SENT:
            d.sent_at = d.sent_at or now
            d.error = ""
        elif status == Delivery.Status.DELIVERED:
            d.delivered_at = now
        elif status == Delivery.Status.FAILED:
            d.failed_at = now
            d.error = error[:2000]
        d.save()


@transaction.atomic
def send_batch(batch: MailBatch, *, user=None) -> MailBatch:
    """Hand every letter to the postal provider. Only a draft batch can be sent."""
    batch = MailBatch.all_associations.select_for_update().get(pk=batch.pk)
    if batch.status != MailBatch.Status.DRAFT:
        raise BatchError(_("This batch has already been sent."))
    _check_gate(batch)
    _check_not_mailed([i.letter for i in batch.items.all()], exclude_batch=batch)
    association = batch.association
    provider = providers.get_provider("PostalProvider", association)
    batch.provider = providers.plugin_for("PostalProvider", association) or "default"
    for item in _items(batch):
        result = provider.send_letter(letter_request(item))
        if result.status == LetterStatus.QUEUED_FOR_PRINT:
            item.status = MailItem.Status.QUEUED_FOR_PRINT
        elif result.status == LetterStatus.SUBMITTED:
            item.status = MailItem.Status.SUBMITTED
            item.provider_id = result.reference[:200]
            item.tracking = result.tracking[:200]
            _mark_deliveries(item, Delivery.Status.SENT, provider_id=result.reference)
        else:
            item.status = MailItem.Status.FAILED
            item.error = result.message
            _mark_deliveries(item, Delivery.Status.FAILED, error=result.message or "failed")
        item.save()
    batch.status = MailBatch.Status.READY if provider.prints_locally else MailBatch.Status.SUBMITTED
    batch.sent_at = timezone.now()
    batch.save(update_fields=["status", "provider", "sent_at"])
    audit.record("mail_batch.send", batch, changes={"provider": batch.provider}, actor=user)
    return batch


@transaction.atomic
def confirm_mailed(batch: MailBatch, *, user=None) -> MailBatch:
    """The board printed the batch and mailed it by hand."""
    batch = MailBatch.all_associations.select_for_update().get(pk=batch.pk)
    if batch.status != MailBatch.Status.READY:
        raise BatchError(_("Only a batch that is ready to print can be marked mailed."))
    _check_gate(batch)
    for item in batch.items.filter(status=MailItem.Status.QUEUED_FOR_PRINT):
        item.status = MailItem.Status.MAILED_BY_HAND
        item.save(update_fields=["status", "updated_at"])
        _mark_deliveries(item, Delivery.Status.SENT, provider_id=f"print-batch:{batch.pk}")
    batch.status = MailBatch.Status.MAILED
    batch.confirmed_at = timezone.now()
    batch.confirmed_by = user if user is not None and user.is_authenticated else None
    batch.save(update_fields=["status", "confirmed_at", "confirmed_by"])
    audit.record("mail_batch.mailed", batch, actor=user)
    return batch


# --- tracking ---------------------------------------------------------------------------

IN_TRANSIT = "in_transit"
DELIVERED = "delivered"
RETURNED = "returned"
FAILED = "failed"
_FINAL = {MailItem.Status.DELIVERED, MailItem.Status.RETURNED, MailItem.Status.FAILED}


def record_tracking_event(association, provider_id: str, kind: str) -> MailItem | None:
    """Apply a mail-service tracking event. Unknown ids are ignored; repeats are no-ops."""
    if not provider_id:
        return None
    item = (
        MailItem.all_associations.filter(association=association, provider_id=provider_id)
        .select_related("batch")
        .first()
    )
    if item is None:
        return None
    if kind == IN_TRANSIT:
        if item.status == MailItem.Status.SUBMITTED:
            item.status = MailItem.Status.IN_TRANSIT
            item.save(update_fields=["status", "updated_at"])
    elif kind == DELIVERED:
        if item.status not in _FINAL:
            item.status = MailItem.Status.DELIVERED
            item.save(update_fields=["status", "updated_at"])
            _mark_deliveries(item, Delivery.Status.DELIVERED)
    elif kind in (RETURNED, FAILED) and item.status not in (
        MailItem.Status.RETURNED,
        MailItem.Status.FAILED,
    ):
        item.status = MailItem.Status.RETURNED if kind == RETURNED else MailItem.Status.FAILED
        item.error = _("Returned to sender") if kind == RETURNED else _("Mail service failed")
        item.save(update_fields=["status", "error", "updated_at"])
        _mark_deliveries(item, Delivery.Status.FAILED, error=item.error)
    return item


def file_bytes(batch: MailBatch, which: str) -> bytes:
    key = batch.pdf_key if which == "pdf" else batch.manifest_key
    return providers.get_provider("FileStorage", batch.association).get(key)
