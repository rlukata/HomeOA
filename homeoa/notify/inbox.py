"""The signed-in user's in-app notifications."""

from __future__ import annotations

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm, roles_for
from homeoa.core import residents_enabled
from homeoa.registry.models import Person

from . import channels as ch
from .models import Delivery, Notification


def is_board_view(request) -> bool:
    association = getattr(request, "association", None)
    return has_perm(request.user, perms.BOARD_SHELL, association=association)


def can_see(request) -> bool:
    """Board users and System admins always; residents only while residents are on."""
    association = getattr(request, "association", None)
    user = getattr(request, "user", None)
    if association is None or user is None or not user.is_authenticated:
        return False
    return (
        is_board_view(request)
        or perms.SYS_ADMIN in roles_for(user, association)
        or residents_enabled(association)
    )


def for_user(request):
    """In-app notifications addressed to the user's Person, newest first.

    Deliveries held as ``preview`` (resident gate, preview-only callers) were never sent,
    so they are left out.
    """
    person = Person.objects.filter(user=request.user).first()
    if person is None:
        return Notification.objects.none()
    sent = Delivery.objects.filter(channel=ch.INAPP).exclude(status=Delivery.Status.PREVIEW)
    return Notification.objects.filter(
        recipient=person, pk__in=sent.values("notification_id")
    ).order_by("-created_at", "-pk")


def unread_count(request) -> int | None:
    """Unread count for the header, or None when the bell should not show."""
    if not can_see(request):
        return None
    return for_user(request).filter(read_at__isnull=True).count()
