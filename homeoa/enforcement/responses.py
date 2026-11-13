"""Owners and tenants see and answer violations at their unit (VIO-06).

A violation shows in the resident's Requests tab once its first notice has gone out. They
see the rule, description, photos, notices, cure deadline and hearings; owners also see
fines and the notice letters. The reporter is never shown. A response ("I fixed it" or "I
disagree", with photos) notifies the board; "I disagree" with no hearing scheduled puts a
"offer a hearing" to-do on Home. Nothing about the violation changes until the board acts.
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from . import services
from .models import Hearing, ResponsePhoto, Violation, ViolationResponse

RESPONSE_TYPE = "enforcement.response"


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="violations",
            key=RESPONSE_TYPE,
            label=gettext_lazy("Response to a violation"),
            default_channels=(ch.EMAIL, ch.INAPP),
            audience=ntypes.AUDIENCE_BOARD,
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with u=unit k=kind %}"
                "Unit {{ u }} answered a violation: {{ k }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=name c=category %}"
                "{{ n }} wrote about the {{ c }} violation:{% endblocktranslate %}\n\n"
                "{{ message }}\n\n{{ url }}"
            ),
        )
    )


def visible_to(user, association):
    """Violations at the user's units (owned or lived in) whose first notice went out."""
    from homeoa.access.services import units_for

    units = units_for(user, association)
    return (
        Violation.all_associations.filter(association=association, unit__in=units)
        .filter(steps__isnull=False)
        .distinct()
        .select_related("unit", "category")
    )


def is_owner(user, violation: Violation) -> bool:
    from homeoa.portal.views.home import is_owner_of
    from homeoa.registry.services import today_for

    return is_owner_of(user, violation.unit, today_for(violation.association))


def can_respond(violation: Violation) -> bool:
    return violation.status == Violation.Status.OPEN


@transaction.atomic
def respond(violation: Violation, *, user, person, kind: str, message: str, photos=()):
    """Record a response with ``photos`` (``(filename, bytes)`` pairs) and tell the board."""
    from homeoa.documents import services as documents
    from homeoa.documents.models import Category, Visibility
    from homeoa.notify.services import board_recipients, notify_after_commit

    if not can_respond(violation):
        raise services.EnforcementError(_("This violation is closed."))
    if kind not in ViolationResponse.Kind.values:
        raise services.EnforcementError(_("Choose a response."))
    message = (message or "").strip()
    if not message:
        raise services.EnforcementError(_("Write a few words for the board."))
    response = ViolationResponse.all_associations.create(
        association=violation.association,
        violation=violation,
        kind=kind,
        message=message,
        person=person,
        submitted_by=user,
    )
    for filename, content in photos:
        documents.check_photo(filename, len(content))
        version = documents.attach(
            violation.association,
            filename=filename,
            content=content,
            title=_("Response photo: %(unit)s") % {"unit": violation.unit},
            category=Category.OTHER,
            visibility=Visibility.BOARD,
            user=user,
        )
        ResponsePhoto.all_associations.create(
            association=violation.association, response=response, document=version.document
        )
    notify_after_commit(
        RESPONSE_TYPE,
        board_recipients(violation.association),
        {
            "unit": violation.unit.number,
            "kind": response.get_kind_display(),
            "name": person.full_name if person else user.email,
            "category": str(violation.category),
            "message": message,
            "url": getattr(settings, "HOMEOA_BASE_URL", "")
            + reverse("enforcement:detail", args=[violation.pk]),
            "association_name": violation.association.name,
        },
        obj=response,
        association=violation.association,
    )
    return response


def disputes_without_hearing(association) -> list[Violation]:
    """Open violations someone disagreed with that have no hearing scheduled."""
    return list(
        Violation.all_associations.filter(
            association=association,
            status=Violation.Status.OPEN,
            responses__kind=ViolationResponse.Kind.DISAGREE,
        )
        .exclude(
            pk__in=Hearing.all_associations.filter(cancelled_on__isnull=True).values("violation_id")
        )
        .distinct()
        .select_related("unit", "category")
    )
