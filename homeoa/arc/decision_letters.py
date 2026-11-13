"""ARC-04: decision letters.

On a decision, once it commits, the approval, approval-with-conditions or denial
letter is generated through ``letters.generate_letter``. The ``Letter`` row carries the
unit, so it lands on the unit file (the ``letters`` contributor). Delivery follows e-notice consent:
owners with fresh e-delivery consent get an email copy with the PDF attached; when any
current owner has none, the letter goes into a mail batch for the board to print or send.
A failure is logged and audited (``arc.decision_letter_failed``), never raised.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from .models import ArcRequest

logger = logging.getLogger(__name__)

DECIDED = "arc.decided"

_CLOSING = "Sincerely,\nThe Board of Directors\n{{ association.name }}"

# Draft wording, not legal advice (``draft_wording``); the board edits it under Letters.
TEMPLATES = {
    ArcRequest.Status.APPROVED: {
        "key": "arc-approval",
        "name": gettext_lazy("Architectural approval"),
        "subject": gettext_lazy("Architectural request approved: unit {{ unit.number }}"),
        "body": gettext_lazy(
            "Dear {{ owner.name }},\n\n"
            "On {{ arc.decided_on }} the board approved your architectural request "
            "“{{ arc.title }}” for unit {{ unit.number }}, submitted on "
            "{{ arc.submitted_on }}.\n\n"
            "{% if arc.citations %}This decision relies on: {{ arc.citations }}\n\n{% endif %}"
            "{% if arc.note %}{{ arc.note }}\n\n{% endif %}"
            "Please keep this letter with your records for the unit.\n\n"
        )
        + _CLOSING,
    },
    ArcRequest.Status.APPROVED_WITH_CONDITIONS: {
        "key": "arc-approval-conditions",
        "name": gettext_lazy("Architectural approval with conditions"),
        "subject": gettext_lazy(
            "Architectural request approved with conditions: unit {{ unit.number }}"
        ),
        "body": gettext_lazy(
            "Dear {{ owner.name }},\n\n"
            "On {{ arc.decided_on }} the board approved your architectural request "
            "“{{ arc.title }}” for unit {{ unit.number }}, submitted on "
            "{{ arc.submitted_on }}, on these conditions:\n\n"
            "{{ arc.conditions }}\n\n"
            "{% if arc.citations %}This decision relies on: {{ arc.citations }}\n\n{% endif %}"
            "{% if arc.note %}{{ arc.note }}\n\n{% endif %}"
            "The approval holds only while the work meets these conditions. Please keep this "
            "letter with your records for the unit.\n\n"
        )
        + _CLOSING,
    },
    ArcRequest.Status.DENIED: {
        "key": "arc-denial",
        "name": gettext_lazy("Architectural denial"),
        "subject": gettext_lazy("Architectural request denied: unit {{ unit.number }}"),
        "body": gettext_lazy(
            "Dear {{ owner.name }},\n\n"
            "On {{ arc.decided_on }} the board denied your architectural request "
            "“{{ arc.title }}” for unit {{ unit.number }}, submitted on "
            "{{ arc.submitted_on }}.\n\n"
            "{% if arc.citations %}This decision relies on: {{ arc.citations }}\n\n{% endif %}"
            "{% if arc.note %}{{ arc.note }}\n\n{% endif %}"
            "If you have questions or would like to submit a revised request, contact the "
            "board{% if association.email %} at {{ association.email }}{% endif %}.\n\n"
        )
        + _CLOSING,
    },
}

# Approved by law, not by the board (``arc.deemed``).
DEEMED_TEMPLATE = {
    "key": "arc-deemed-approval",
    "name": gettext_lazy("Architectural approval by law (not denied in time)"),
    "subject": gettext_lazy("Architectural request approved: unit {{ unit.number }}"),
    "body": gettext_lazy(
        "Dear {{ owner.name }},\n\n"
        "Your architectural request “{{ arc.title }}” for unit {{ unit.number }}, received "
        "on {{ arc.submitted_on }}, was not denied in writing within the time the law "
        "allows. It is therefore approved as of {{ arc.decided_on }}"
        "{% if arc.citations %} ({{ arc.citations }}){% endif %}.\n\n"
        "Please keep this letter with your records for the unit.\n\n"
    )
    + _CLOSING,
}

_FIELDS = {
    "title": (gettext_lazy("Architectural request title"), "Blue front door"),
    "decision": (gettext_lazy("Architectural decision"), "Approved with conditions"),
    "conditions": (gettext_lazy("Conditions of approval"), "Use the approved paint color."),
    "citations": (gettext_lazy("Rules the decision relies on"), "Declaration §8.2"),
    "note": (gettext_lazy("Note from the board"), ""),
    "submitted_on": (gettext_lazy("Architectural request submitted on"), "October 1, 2026"),
    "decided_on": (gettext_lazy("Architectural decision date"), "October 20, 2026"),
}


def register() -> None:
    """Merge fields and the notification type (called from ``ready``)."""
    from homeoa.letters import merge

    for name, (label, sample) in _FIELDS.items():
        merge.register(
            f"arc.{name}",
            label,
            lambda ctx, name=name: ctx.extra.get(f"arc_{name}", ""),
            sample=sample,
            module="arc",
        )
    ntypes.register(
        ntypes.NotificationType(
            module="arc",
            key=DECIDED,
            label=gettext_lazy("Architectural request decision letter"),
            default_channels=(ch.EMAIL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            subject_template="{{ subject }}",
            body_template=(
                "{% load i18n %}{% blocktranslate %}The board has decided your architectural"
                " request “{{ title }}” ({{ unit }}): {{ decision }}. The decision letter is"
                " attached.{% endblocktranslate %}"
            ),
        )
    )


def template_for(association, outcome: str, *, deemed: bool = False):
    from homeoa.letters import services as letters
    from homeoa.letters.models import LetterTemplate

    spec = DEEMED_TEMPLATE if deemed else TEMPLATES[outcome]
    template = LetterTemplate.all_associations.filter(
        association=association, key=spec["key"]
    ).first()
    if template is None:
        template = letters.create_template(
            association,
            key=spec["key"],
            name=str(spec["name"]),
            subject=str(spec["subject"]),
            body=str(spec["body"]),
            draft_wording=True,
        )
    return template


def merge_values(req: ArcRequest) -> dict:
    return {
        "arc_title": req.title,
        "arc_decision": str(ArcRequest.Status(req.status).label),
        "arc_conditions": req.conditions,
        "arc_citations": req.citations,
        "arc_note": req.decision_note,
        "arc_submitted_on": date_format(req.submitted_on, "F j, Y"),
        "arc_decided_on": date_format(req.decided_on, "F j, Y") if req.decided_on else "",
    }


def schedule(req: ArcRequest, *, user=None) -> None:
    """Generate and deliver the decision letter once the decision commits."""
    pk = req.pk
    transaction.on_commit(lambda: send_decision_letter(pk, user=user))


def send_decision_letter(pk: int, *, user=None):
    req = ArcRequest.all_associations.select_related("association", "unit").get(pk=pk)
    try:
        with transaction.atomic():
            return _send(req, user=user)
    except Exception as exc:  # noqa: BLE001 - alerts never raise
        logger.exception("ARC decision letter for request %s failed", pk)
        audit.record(
            "arc.decision_letter_failed",
            req,
            association=req.association,
            message=str(exc)[:500],
        )
        return None


def _send(req: ArcRequest, *, user=None):
    from homeoa.letters import batches
    from homeoa.letters import services as letters
    from homeoa.notify import services as nsvc
    from homeoa.registry.services import current_owners

    if req.status not in ArcRequest.DECISIONS:
        return None
    association = req.association
    template = template_for(association, req.status, deemed=req.deemed)
    letter = letters.generate_letter(
        template, req.unit, as_of=req.decided_on, user=user, **merge_values(req)
    )
    req.decision_letter = letter
    req.save(update_fields=["decision_letter"])
    audit.record(
        "arc.decision_letter",
        req,
        association=association,
        changes={"letter": letter.pk, "template": template.key},
        actor=user,
    )
    owners = list(current_owners(req.unit, req.decided_on))
    ntype = ntypes.get(DECIDED)
    email = [p for p in owners if ch.EMAIL in nsvc.channels_for(ntype, p)]
    paper = [p for p in owners if p not in email]
    if email:
        nsvc.notify(
            DECIDED,
            email,
            {
                "subject": letter.subject,
                "title": req.title,
                "unit": str(req.unit),
                "decision": str(ArcRequest.Status(req.status).label),
            },
            req.decided_on,
            obj=req,
            period=f"letter:{letter.pk}",
            association=association,
            unit=req.unit,
            attachments=[
                {
                    "name": f"{template.key}-{req.unit.number}.pdf",
                    "content_type": "application/pdf",
                    "file_key": letter.file_key,
                }
            ],
        )
    batch = None
    if paper or not owners:
        batch = batches.create_batch(
            association,
            [letter],
            name=_("Architectural decision: %(unit)s") % {"unit": req.unit},
            user=user,
        )
    return letter, batch
