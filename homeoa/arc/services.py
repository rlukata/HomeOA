"""Architectural review services: forms, submission, comments, votes and the decision.

Reviewers are the current board roster, or the members of one governance committee when
the association names it (``Association.settings["arc_reviewer_committee"]``, a committee
pk). The decision is recorded by a person with ``architectural.decide``; votes are shown
(with the majority of votes cast) but never decide anything on their own.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.core.context import association_today
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from . import deadlines, deemed
from .models import (
    ArcAttachment,
    ArcComment,
    ArcForm,
    ArcRequest,
    ArcVote,
    FieldKind,
    StatutoryKind,
    VoteChoice,
)

REVIEWER_SETTING = "arc_reviewer_committee"
INFO_REQUESTED = "arc.info_requested"

DEFAULT_FORM_NAME = "Exterior change"


class ArcError(Exception):
    pass


def default_form_fields() -> list[dict]:
    # Labels are stored data (the board edits them), so they are seeded in English.
    return [
        {"key": "description", "label": "Describe the change", "kind": "textarea",
         "required": True},
        {"key": "location", "label": "Where on the property", "kind": "text", "required": True},
        {"key": "contractor", "label": "Contractor (if any)", "kind": "text", "required": False},
        {"key": "start_date", "label": "Planned start date", "kind": "date", "required": False},
        {"key": "category", "label": "Type of change", "kind": "choice", "required": True,
         "choices": ["Paint or color", "Windows or doors", "Landscaping", "Structure", "Other"]},
        {"key": "neighbors", "label": "I have told neighbors who may be affected",
         "kind": "checkbox", "required": False},
    ]  # fmt: skip


def ensure_default_form(association) -> ArcForm:
    form = ArcForm.all_associations.filter(association=association).order_by("pk").first()
    if form is None:
        form = ArcForm.all_associations.create(
            association=association, name=DEFAULT_FORM_NAME, fields=default_form_fields()
        )
    return form


# ------------------------------------------------------------------ form definitions


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "field"


def validate_fields(defs: list[dict]) -> list[dict]:
    """Normalize and check field definitions; raise ``ArcError`` on a bad one."""
    kinds = set(FieldKind.values)
    out, keys = [], set()
    for raw in defs:
        label = str(raw.get("label", "")).strip()[:200]
        if not label:
            raise ArcError(_("Every field needs a label."))
        kind = raw.get("kind", "")
        if kind not in kinds:
            raise ArcError(_("Unknown field type: %(kind)s") % {"kind": kind})
        key = _slug(str(raw.get("key") or label))
        base, n = key, 2
        while key in keys:
            key, n = f"{base}_{n}", n + 1
        keys.add(key)
        field = {"key": key, "label": label, "kind": kind, "required": bool(raw.get("required"))}
        if kind == FieldKind.CHOICE:
            choices = [str(c).strip()[:120] for c in raw.get("choices") or [] if str(c).strip()]
            if len(choices) < 2:
                raise ArcError(
                    _("The choice field “%(label)s” needs at least two options.") % {"label": label}
                )
            field["choices"] = choices
        if raw.get("help"):
            field["help"] = str(raw["help"]).strip()[:300]
        out.append(field)
    if not out:
        raise ArcError(_("A form needs at least one field."))
    return out


def save_form(
    association, *, name, fields, description="", active=True, form=None, statutory_kind=""
) -> ArcForm:
    name = (name or "").strip()[:120]
    if not name:
        raise ArcError(_("Give the form a name."))
    fields = validate_fields(fields)
    clash = ArcForm.all_associations.filter(association=association, name=name)
    if form is not None:
        clash = clash.exclude(pk=form.pk)
    if clash.exists():
        raise ArcError(_("A form with this name already exists."))
    form = form or ArcForm(association=association)
    form.name, form.fields, form.description, form.active = name, fields, description, active
    form.statutory_kind = statutory_kind if statutory_kind in StatutoryKind.values else ""
    form.save()
    return form


def clean_answers(defs: list[dict], data) -> tuple[dict, dict]:
    """Validate posted ``data`` (a QueryDict or dict) against ``defs``.

    Returns ``(answers, errors)`` keyed by field key. Dates are stored as ISO strings.
    """
    answers, errors = {}, {}
    for f in defs:
        key, kind = f["key"], f["kind"]
        raw = data.get(key, "")
        raw = raw.strip() if isinstance(raw, str) else raw
        if kind == FieldKind.CHECKBOX:
            value = raw in (True, "on", "1", "true", "yes")
            if f.get("required") and not value:
                errors[key] = _("Please confirm this.")
            answers[key] = value
            continue
        if not raw:
            if f.get("required"):
                errors[key] = _("This field is required.")
            answers[key] = ""
            continue
        if kind == FieldKind.DATE:
            try:
                d = parse_date(raw) if isinstance(raw, str) else None
            except ValueError:
                d = None
            if d is None:
                errors[key] = _("Enter a valid date.")
                continue
            answers[key] = d.isoformat()
        elif kind == FieldKind.CHOICE:
            if raw not in f.get("choices", []):
                errors[key] = _("Choose one of the options.")
                continue
            answers[key] = raw
        else:
            limit = 5000 if kind == FieldKind.TEXTAREA else 300
            answers[key] = str(raw)[:limit]
    return answers, errors


# ------------------------------------------------------------------ reviewers


def reviewer_committee(association):
    from homeoa.governance.models import Committee

    pk = (association.settings or {}).get(REVIEWER_SETTING)
    if not pk:
        return None
    return Committee.all_associations.filter(association=association, pk=pk, active=True).first()


def set_reviewer_committee(association, committee) -> None:
    data = dict(association.settings or {})
    if committee is None:
        data.pop(REVIEWER_SETTING, None)
    else:
        data[REVIEWER_SETTING] = committee.pk
    association.settings = data
    association.save(update_fields=["settings"])


def reviewers(association, on: dt.date | None = None) -> list:
    """People who vote on requests: the ARC committee's members, else the board roster."""
    from homeoa.governance import services as gov

    on = on or association_today(association)
    committee = reviewer_committee(association)
    if committee is not None:
        people = [m.person for m in gov._active(committee.members.select_related("person"), on)]
    else:
        people = [t.person for t in gov.roster(association, on)]
    seen, out = set(), []
    for p in people:  # one seat per person, even with two offices
        if p.pk not in seen:
            seen.add(p.pk)
            out.append(p)
    return out


def reviewer_for(user, association):
    """The reviewer Person linked to ``user``, or ``None``."""
    if not getattr(user, "is_authenticated", False):
        return None
    return next((p for p in reviewers(association) if p.user_id == user.pk), None)


def tally(req: ArcRequest, association=None) -> dict:
    association = association or req.association
    votes = list(req.votes.select_related("reviewer"))
    counts = Counter(v.vote for v in votes)
    cast = [v for v in votes if v.vote != VoteChoice.ABSTAIN]
    majority = None
    if cast:
        top, n = Counter(v.vote for v in cast).most_common(1)[0]
        if n * 2 > len(cast):
            majority = VoteChoice(top).label
    return {
        "votes": votes,
        "in": len(votes),
        "of": len(reviewers(association)),
        "counts": [(c.label, counts.get(c.value, 0)) for c in VoteChoice],
        "majority": majority,
    }


# ------------------------------------------------------------------ lifecycle


def _require_open(req: ArcRequest) -> None:
    if not req.is_open:
        raise ArcError(_("This request is closed."))


@transaction.atomic
def submit(
    association, *, unit, person, form: ArcForm, title, data, files=(), user=None
) -> ArcRequest:
    """Create a request. ``files`` are ``(filename, bytes)`` pairs. Raises ``ArcError``
    with ``.errors`` (field key → message) when answers don't validate."""
    from homeoa.documents import services as docs
    from homeoa.documents.models import Visibility

    if unit.association_id != association.pk or person.association_id != association.pk:
        raise ArcError(_("That unit or person belongs to another association."))
    if not form.active:
        raise ArcError(_("This form is not in use."))
    title = (title or "").strip()[:200]
    answers, errors = clean_answers(form.fields, data)
    if not title:
        errors["title"] = _("Give the request a short title.")
    if errors:
        exc = ArcError(_("Please fix the highlighted fields."))
        exc.errors = errors
        raise exc
    req = ArcRequest.objects.create(
        association=association,
        unit=unit,
        submitted_by=person,
        entered_by=user if getattr(user, "is_authenticated", False) else None,
        form=form,
        form_fields=form.fields,
        statutory_kind=form.statutory_kind,
        title=title,
        answers=answers,
        submitted_on=association_today(association),
    )
    deadlines.start_clock(req, req.submitted_on)
    deemed.update(req)
    req.save(update_fields=["clock_started_on", "decide_by", "deny_by"])
    for filename, content in files:
        version = docs.attach(
            association,
            filename=filename,
            content=content,
            title=_("Architectural request %(n)s: %(name)s") % {"n": req.pk, "name": filename},
            visibility=Visibility.BOARD,
            user=user,
        )
        ArcAttachment.objects.create(association=association, request=req, version=version)
    return req


def add_comment(req: ArcRequest, *, body, user, internal=True) -> ArcComment:
    body = (body or "").strip()[:5000]
    if not body:
        raise ArcError(_("Write a comment first."))
    return ArcComment.objects.create(
        association=req.association,
        request=req,
        author=user,
        author_name=(getattr(user, "full_name", "") or getattr(user, "email", ""))[:200],
        body=body,
        internal=internal,
    )


@transaction.atomic
def cast_vote(req: ArcRequest, *, reviewer, vote, note="") -> ArcVote:
    _require_open(req)
    if vote not in VoteChoice.values:
        raise ArcError(_("Choose a vote."))
    if reviewer.pk not in {p.pk for p in reviewers(req.association)}:
        raise ArcError(_("Only current reviewers can vote."))
    obj, _created = ArcVote.objects.update_or_create(
        association=req.association,
        request=req,
        reviewer=reviewer,
        defaults={"vote": vote, "note": (note or "").strip()[:2000]},
    )
    if req.status == ArcRequest.Status.SUBMITTED:
        req.status = ArcRequest.Status.UNDER_REVIEW
        req.save(update_fields=["status"])
    return obj


@transaction.atomic
def request_info(req: ArcRequest, *, message, user) -> ArcComment:
    """Ask the requester for more information: visible comment, status change, and a
    notification to the requester once the change commits."""
    from homeoa.notify.services import notify_after_commit

    _require_open(req)
    comment = add_comment(req, body=message, user=user, internal=False)
    req.status = ArcRequest.Status.INFO_REQUESTED
    deemed.info_requested(req, association_today(req.association))
    req.save(update_fields=["status", "info_requested_on"])
    url = getattr(settings, "HOMEOA_BASE_URL", "") + reverse("arc:mine_detail", args=[req.pk])
    notify_after_commit(
        INFO_REQUESTED,
        [req.submitted_by],
        {"title": req.title, "unit": str(req.unit), "message": comment.body, "url": url},
        obj=comment,
        association=req.association,
        unit=req.unit,
    )
    return comment


@transaction.atomic
def provide_info(req: ArcRequest, *, message, user) -> ArcComment:
    """The requester answers: visible comment, back to review. Answering a request for
    more information restarts the decision clock (``decide_by``)."""
    _require_open(req)
    comment = add_comment(req, body=message, user=user, internal=False)
    if req.status == ArcRequest.Status.INFO_REQUESTED:
        today = association_today(req.association)
        req.status = ArcRequest.Status.UNDER_REVIEW
        deadlines.start_clock(req, today)
        deemed.info_provided(req, today)
        deemed.update(req)
        req.save(
            update_fields=[
                "status",
                "clock_started_on",
                "decide_by",
                "info_requested_on",
                "paused_days",
                "deny_by",
            ]
        )
    return comment


@transaction.atomic
def decide(req: ArcRequest, *, outcome, user, conditions="", note="", citations="") -> ArcRequest:
    """Record the decision; the decision letter follows after commit."""
    from . import decision_letters

    _require_open(req)
    if outcome not in ArcRequest.DECISIONS:
        raise ArcError(_("Choose a decision."))
    conditions = (conditions or "").strip()[:5000]
    if outcome == ArcRequest.Status.APPROVED_WITH_CONDITIONS and not conditions:
        raise ArcError(_("List the conditions of approval."))
    if outcome != ArcRequest.Status.APPROVED_WITH_CONDITIONS:
        conditions = ""
    req.status = outcome
    req.conditions = conditions
    req.decision_note = (note or "").strip()[:5000]
    req.citations = (citations or "").strip()[:2000]
    req.decided_on = association_today(req.association)
    req.decided_by = user
    req.save()
    decision_letters.schedule(req, user=user)
    return req


@transaction.atomic
def withdraw(req: ArcRequest, *, user) -> ArcRequest:
    _require_open(req)
    req.status = ArcRequest.Status.WITHDRAWN
    req.save(update_fields=["status"])
    audit.record("arc.withdrawn", req, actor=user)
    return req


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="arc",
            key=INFO_REQUESTED,
            label=gettext_lazy("Architectural request: information requested"),
            default_channels=(ch.INAPP, ch.EMAIL),
            audience=ntypes.AUDIENCE_ALL,
            subject_template=(
                "{% load i18n %}{% blocktranslate %}More information needed: {{ title }}"
                "{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate %}The board needs more information about"
                " your architectural request “{{ title }}” ({{ unit }}).{% endblocktranslate %}"
                "\n\n{{ message }}\n\n{{ url }}"
            ),
        )
    )
