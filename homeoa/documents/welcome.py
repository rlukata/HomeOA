"""Owners' welcome packet (RES-03): a Welcome page in the portal for owners (not tenants).

The board writes the message and marks documents for the packet (Documents > Welcome
packet; a document's page). Owners open it from More > Welcome packet; Home shows a card
for it until they have. The invitation email to an owner mentions it.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from homeoa import audit

from .models import Document, Visibility, WelcomePacket, WelcomeSeen
from .services import UploadRejected


def packet(association) -> WelcomePacket:
    obj, _created = WelcomePacket.all_associations.get_or_create(association=association)
    return obj


def set_message(association, message: str, *, user=None) -> WelcomePacket:
    """Change the board's welcome message. Audited through the model."""
    obj = packet(association)
    obj.message = message.strip()
    obj.save()
    return obj


def documents(association):
    """The packet's documents that owners can open, governing documents first."""
    return (
        Document.all_associations.filter(
            association=association,
            in_welcome_packet=True,
            current_version__isnull=False,
        )
        .exclude(visibility=Visibility.BOARD)
        .select_related("current_version")
        .order_by("category", "title")
    )


def set_in_packet(document: Document, include: bool, *, user=None) -> bool:
    """Add a document to the packet or take it out. Board-only documents can't be added."""
    include = bool(include)
    if include and document.visibility == Visibility.BOARD:
        raise UploadRejected(
            _("Board-only documents can't go in the welcome packet. Change who can see it first.")
        )
    if document.in_welcome_packet == include:
        return False
    document.in_welcome_packet = include
    document.save(update_fields=["in_welcome_packet", "updated_at"])
    audit.record(
        "documents.welcome_packet",
        document,
        actor=user,
        changes={"in_welcome_packet": include},
    )
    return True


def has_seen(user, association) -> bool:
    return WelcomeSeen.all_associations.filter(association=association, user=user).exists()


def mark_seen(user, association) -> None:
    if has_seen(user, association):
        return
    try:
        with transaction.atomic():
            WelcomeSeen.all_associations.create(association=association, user=user)
    except IntegrityError:
        pass  # opened twice at once
