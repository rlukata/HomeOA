"""Governance services: roster on a date, resolution numbering and lifecycle, action items."""

from __future__ import annotations

import datetime

from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils.translation import gettext as _

from homeoa import audit

from .models import (
    ActionItem,
    BoardTerm,
    Committee,
    CommitteeMember,
    Resolution,
    ResolutionVote,
    Visibility,
    Vote,
)

# Association setting (``Association.settings``): days before a term ends to show a to-do.
TERM_END_SETTING = "governance_term_end_notice_days"
DEFAULT_TERM_END_DAYS = 60


class GovernanceError(Exception):
    pass


def _active(qs, on: datetime.date):
    return qs.filter(start__lte=on).filter(Q(end__isnull=True) | Q(end__gte=on))


def roster(association, on: datetime.date) -> list[BoardTerm]:
    """Board terms in effect on ``on``, officers first."""
    order = {"president": 0, "vice_president": 1, "secretary": 2, "treasurer": 3, "director": 4}
    terms = _active(
        BoardTerm.all_associations.filter(association=association).select_related("person"),
        on,
    )
    return sorted(terms, key=lambda t: (order.get(t.office, 9), t.person.full_name, t.pk))


def is_board_member(person, on: datetime.date) -> bool:
    return _active(BoardTerm.all_associations.filter(person=person), on).exists()


def committees(association, on: datetime.date) -> list[dict]:
    out = []
    for c in Committee.all_associations.filter(association=association, active=True):
        members = list(
            _active(
                CommitteeMember.all_associations.filter(committee=c).select_related("person"), on
            )
        )
        out.append(
            {
                "committee": c,
                "chair": next((m for m in members if m.is_chair), None),
                "members": members,
            }
        )
    return out


def add_term(association, *, person, office, start, end=None, note="") -> BoardTerm:
    term = BoardTerm(
        association=association, person=person, office=office, start=start, end=end, note=note
    )
    term.full_clean(exclude=["association"])
    term.save()
    return term


def add_committee_member(committee, *, person, start, end=None, is_chair=False):
    m = CommitteeMember(
        association=committee.association,
        committee=committee,
        person=person,
        start=start,
        end=end,
        is_chair=is_chair,
    )
    m.full_clean(exclude=["association"])
    m.save()
    return m


def term_end_days(association) -> int:
    try:
        return int((association.settings or {}).get(TERM_END_SETTING, DEFAULT_TERM_END_DAYS))
    except TypeError, ValueError:
        return DEFAULT_TERM_END_DAYS


def ending_terms(association, today: datetime.date) -> list[BoardTerm]:
    """Current terms that end within the notice window (setting, default 60 days)."""
    horizon = today + datetime.timedelta(days=term_end_days(association))
    return list(
        BoardTerm.all_associations.filter(
            association=association, start__lte=today, end__gte=today, end__lte=horizon
        )
        .select_related("person")
        .order_by("end", "pk")
    )


# ---------------------------------------------------------------- resolutions


def next_seq(association, year: int) -> int:
    top = Resolution.all_associations.filter(association=association, year=year).aggregate(
        m=Max("seq")
    )["m"]
    return (top or 0) + 1


def create_resolution(association, *, title, text="", year=None, user=None) -> Resolution:
    """New draft, numbered next in ``year`` for this association (``2026-03``)."""
    if year is None:
        from homeoa.core.context import association_today

        year = association_today(association).year
    for _attempt in range(3):
        try:
            with transaction.atomic():
                return Resolution.objects.create(
                    association=association,
                    year=year,
                    seq=next_seq(association, year),
                    title=title,
                    text=text,
                    created_by=user,
                )
        except IntegrityError:
            continue
    raise GovernanceError(_("Could not number the resolution; try again."))


def update_draft(resolution: Resolution, *, title, text) -> Resolution:
    if not resolution.is_draft:
        raise GovernanceError(_("An adopted resolution cannot be edited."))
    resolution.title = title
    resolution.text = text
    resolution.save()
    return resolution


def record_votes(resolution: Resolution, votes: dict) -> None:
    """``votes``: {Person: "for" | "against" | "abstain" | ""}. Empty removes the vote."""
    if not resolution.is_draft:
        raise GovernanceError(_("Votes are locked once a resolution is adopted."))
    with transaction.atomic():
        for person, value in votes.items():
            existing = ResolutionVote.all_associations.filter(
                resolution=resolution, person=person
            ).first()
            if not value:
                if existing:
                    existing.delete()
                continue
            if value not in Vote.values:
                raise GovernanceError(_("Unknown vote."))
            if existing:
                if existing.vote != value:
                    existing.vote = value
                    existing.save()
            else:
                ResolutionVote.objects.create(
                    association=resolution.association,
                    resolution=resolution,
                    person=person,
                    vote=value,
                )


def tally(resolution: Resolution) -> dict[str, int]:
    counts = {v: 0 for v in Vote.values}
    for v in resolution.votes.all():
        counts[v.vote] += 1
    return counts


@transaction.atomic
def adopt(
    resolution: Resolution,
    *,
    adopted_on: datetime.date,
    pdf=None,
    visibility: str = Visibility.OWNERS,
    user=None,
) -> Resolution:
    """Adopt a draft. The PDF (a Django ``UploadedFile``), when given, is filed in the
    document library (Governing, kind Resolution) with ``visibility``. After this the
    resolution is immutable except for rescinding."""
    if not resolution.is_draft:
        raise GovernanceError(_("This resolution is not a draft."))
    if visibility not in Visibility.values:
        raise GovernanceError(_("Choose who may see the PDF."))
    if pdf is not None:
        name = pdf.name or "resolution.pdf"
        ctype = getattr(pdf, "content_type", "") or ""
        if not name.lower().endswith(".pdf") and ctype != "application/pdf":
            raise GovernanceError(_("Upload the resolution as a PDF."))
        if not name.lower().endswith(".pdf"):
            name = f"{name}.pdf"
        from homeoa.documents.models import Category, GoverningKind
        from homeoa.documents.services import UploadRejected, attach

        try:
            version = attach(
                resolution.association,
                filename=name,
                content=pdf.read(),
                title=_("Resolution %(number)s: %(title)s")
                % {"number": resolution.number, "title": resolution.title},
                category=Category.GOVERNING,
                visibility=visibility,
                governing_kind=GoverningKind.RESOLUTION,
                effective_date=adopted_on,
                user=user,
            )
        except UploadRejected as exc:
            raise GovernanceError(str(exc)) from exc
        resolution.document = version.document
        resolution.pdf_key = version.file_key
        resolution.pdf_filename = name[:255]
    resolution.pdf_visibility = visibility
    resolution.adopted_on = adopted_on
    resolution.status = Resolution.Status.ADOPTED
    resolution.save()
    from homeoa.changes import services as changes

    changes.resolution_adopted(resolution)
    return resolution


@transaction.atomic
def rescind(resolution: Resolution, *, on: datetime.date, reason: str, user=None) -> Resolution:
    if resolution.status != Resolution.Status.ADOPTED:
        raise GovernanceError(_("Only an adopted resolution can be rescinded."))
    reason = (reason or "").strip()
    if not reason:
        raise GovernanceError(_("Give a reason for rescinding."))
    resolution.status = Resolution.Status.RESCINDED
    resolution.rescinded_on = on
    resolution.rescind_reason = reason[:300]
    resolution.save()
    audit.record("governance.resolution_rescinded", resolution, message=reason, actor=user)
    if resolution.document_id:
        from django.urls import reverse

        from homeoa.documents.models import DocStatus
        from homeoa.documents.services import set_status

        set_status(
            resolution.document,
            DocStatus.RESCINDED,
            note=_("Rescinded %(date)s: %(reason)s")
            % {"date": on.isoformat(), "reason": resolution.rescind_reason},
            link=reverse("governance:resolution", kwargs={"pk": resolution.pk}),
            user=user,
        )
    return resolution


# ---------------------------------------------------------------- action items


def add_action_item(
    association, *, title, owner, due_date=None, resolution=None, notes="", on=None
) -> ActionItem:
    from homeoa.core.context import association_today

    on = on or association_today(association)
    if not is_board_member(owner, on):
        raise GovernanceError(_("Action items are assigned to a current board member."))
    return ActionItem.objects.create(
        association=association,
        title=title,
        owner=owner,
        due_date=due_date,
        resolution=resolution,
        notes=notes,
    )


def set_action_status(item: ActionItem, status: str, *, on: datetime.date) -> ActionItem:
    if status not in ActionItem.Status.values:
        raise GovernanceError(_("Unknown status."))
    item.status = status
    item.completed_on = on if status == ActionItem.Status.DONE else None
    item.save()
    return item
