"""Board pages for owner votes: list, create, open, close, paper ballots, result."""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.audit.models import AuditEvent
from homeoa.registry import services as registry
from homeoa.registry.models import Person, Unit

from . import issue, services
from .models import (
    Ballot,
    Channel,
    ClosedVoteError,
    PackKind,
    Quorum,
    Threshold,
    Vote,
    VoteKind,
    VoteStatus,
    Weighting,
)


def receipt_id(ballot: Ballot) -> str:
    """Short, stable receipt id an owner can quote; derived from the stored ballot."""
    raw = f"{ballot.pk}|{ballot.vote_id}|{ballot.unit_id}|{ballot.cast_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:10].upper()


def _tz(association):
    return services._tz(association)


def _local_dt(association, value: str) -> dt.datetime:
    parsed = parse_datetime(value or "")
    if parsed is None:
        raise ValidationError(_("Enter a valid date and time."))
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=_tz(association))
    return parsed


def _error(request, exc) -> None:
    if isinstance(exc, ValidationError):
        messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, str(exc))


def turnout(vote: Vote) -> dict:
    eligible = Unit.all_associations.filter(association=vote.association).count()
    voted = (
        Ballot.all_associations.filter(vote=vote, superseded_by__isnull=True)
        .values("unit")
        .distinct()
        .count()
    )
    return {"voted": voted, "eligible": eligible, "outstanding": max(eligible - voted, 0)}


@require_perm(perms.VOTES_ADMINISTER)
def index(request):
    votes = list(Vote.objects.all())
    rows = [{"vote": v, "turnout": turnout(v)} for v in votes]
    return render(
        request,
        "votes/index.html",
        {
            "rows": rows,
            "kinds": VoteKind.choices,
            "weightings": Weighting.choices,
            "default_weighting": registry.vote_weighting(request.association),
            "thresholds": Threshold.choices,
            "pack_kinds": PackKind.choices,
            "quorums": Quorum.choices,
        },
    )


@require_POST
@require_perm(perms.VOTES_ADMINISTER)
def create(request):
    a = request.association
    p = request.POST
    try:
        questions = [line.strip() for line in p.get("questions", "").splitlines() if line.strip()]
        if not questions:
            raise ValidationError(_("Enter at least one question."))
        quorum_percent = None
        if p.get("quorum") == Quorum.CUSTOM:
            try:
                quorum_percent = Decimal(p.get("quorum_percent", ""))
            except InvalidOperation:
                raise ValidationError(_("Enter the quorum percentage.")) from None
        vote = services.create_vote(
            a,
            title=p.get("title", "").strip(),
            description=p.get("description", "").strip(),
            kind=p.get("kind", VoteKind.WRITTEN),
            opens_at=_local_dt(a, p.get("opens_at", "")),
            closes_at=_local_dt(a, p.get("closes_at", "")),
            questions=[(q, services.yes_no_abstain()) for q in questions],
            weighting=p.get("weighting", ""),
            threshold=p.get("threshold", Threshold.MAJORITY_CAST),
            pack_kind=p.get("pack_kind", ""),
            quorum=p.get("quorum", Quorum.NONE),
            quorum_percent=quorum_percent,
            secret=bool(p.get("secret")),
            user=request.user,
        )
    except ValidationError as exc:
        _error(request, exc)
        return redirect("votes:index")
    messages.success(request, _("Vote saved as a draft."))
    return redirect("votes:detail", pk=vote.pk)


def _audit_labels() -> dict[str, str]:
    return {
        AuditEvent.ACTION_CREATE: _("Created"),
        AuditEvent.ACTION_UPDATE: _("Changed"),
        AuditEvent.ACTION_DELETE: _("Deleted"),
        "votes.opened": _("Vote opened"),
        "votes.ballot_cast": _("Ballot cast"),
        "votes.ballot_replaced": _("Ballot replaced"),
        "votes.vote_closed": _("Vote closed"),
        "votes.ballot_letters": _("Ballot letters sent"),
        "votes.ballot_letters_failed": _("Ballot letters failed"),
    }


def _audit_trail(vote: Vote):
    ballot_ids = [
        str(pk) for pk in Ballot.all_associations.filter(vote=vote).values_list("pk", flat=True)
    ]
    labels = _audit_labels()
    events = list(
        AuditEvent.objects.filter(
            Q(object_type="votes.vote", object_id=str(vote.pk))
            | Q(object_type="votes.ballot", object_id__in=ballot_ids)
        ).order_by("created_at", "pk")
    )
    for e in events:
        e.label = labels.get(e.action, e.action)
    return events


def _owners_by_unit(vote: Vote) -> list[dict]:
    on = timezone.localdate(min(timezone.now(), vote.closes_at), _tz(vote.association))
    out = []
    for unit in sorted(
        Unit.all_associations.filter(association=vote.association),
        key=lambda u: getattr(u, "sort_key", u.number),
    ):
        out.append({"unit": unit, "owners": list(registry.current_owners(unit, on))})
    return out


@require_perm(perms.VOTES_ADMINISTER)
def detail(request, pk):
    vote = get_object_or_404(Vote, pk=pk)
    questions = list(vote.questions.prefetch_related("choices"))
    t = services.tally(vote)
    results = []
    for r in t.questions:
        results.append(
            {
                "question": r.question,
                "choices": [(c, r.totals.get(c.pk)) for c in r.question.choices.all()],
                "cast": r.cast,
                "affirmative": r.affirmative,
                "passed": r.passed,
            }
        )
    voted_units = {
        b.unit_id
        for b in Ballot.all_associations.filter(vote=vote).filter(superseded_by__isnull=True)
    }
    roster = _owners_by_unit(vote)
    for row in roster:
        row["voted"] = row["unit"].pk in voted_units
    rows = services.ballot_rows(vote, include_superseded=True)
    return render(
        request,
        "votes/detail.html",
        {
            "vote": vote,
            "questions": questions,
            "turnout": turnout(vote),
            "tally": t,
            "results": results,
            "ballots": [
                {
                    "ballot": r.ballot,
                    "answers": None
                    if r.answers is None
                    else [r.answers.get(q.pk) for q in questions],
                    "receipt": receipt_id(r.ballot),
                }
                for r in rows
            ],
            "roster": roster,
            "verified": services.verify(vote) if vote.status == VoteStatus.CLOSED else None,
            "audit_events": _audit_trail(vote),
            "is_open": vote.status == VoteStatus.OPEN,
            "is_draft": vote.status == VoteStatus.DRAFT,
            "today": timezone.localdate(timezone.now(), _tz(vote.association)),
        },
    )


@require_POST
@require_perm(perms.VOTES_ADMINISTER)
def open_view(request, pk):
    vote = get_object_or_404(Vote, pk=pk)
    try:
        with transaction.atomic():
            services.open_vote(vote)
            issue.issue_ballots(vote, user=request.user)
    except ValidationError as exc:
        _error(request, exc)
    else:
        messages.success(request, _("Vote opened. Ballots are being sent."))
    return redirect("votes:detail", pk=vote.pk)


@require_POST
@require_perm(perms.VOTES_ADMINISTER)
def close_view(request, pk):
    vote = get_object_or_404(Vote, pk=pk)
    try:
        services.close_vote(vote, user=request.user)
    except (ClosedVoteError, PermissionDenied) as exc:
        _error(request, exc)
    else:
        messages.success(request, _("Vote closed and result recorded."))
    return redirect("votes:detail", pk=vote.pk)


@require_POST
@require_perm(perms.VOTES_ADMINISTER)
def paper(request, pk):
    vote = get_object_or_404(Vote, pk=pk)
    p = request.POST
    try:
        unit = Unit.objects.get(pk=int(p.get("unit", "")))
        person = Person.objects.get(pk=int(p.get("person", "")))
    except ValueError, Unit.DoesNotExist, Person.DoesNotExist:
        messages.error(request, _("Choose the unit and the owner who signed the ballot."))
        return redirect("votes:detail", pk=vote.pk)
    answers = {}
    for q in vote.questions.all():
        value = p.get(f"q{q.pk}")
        if value and value.isdigit():
            answers[q.pk] = int(value)
    received = parse_date(p.get("received", "") or "")
    cast_at = None
    if received is not None:
        cast_at = dt.datetime.combine(received, dt.time(12), tzinfo=_tz(vote.association))
        cast_at = min(max(cast_at, vote.opens_at), vote.closes_at)
    try:
        services.cast_ballot(
            vote,
            unit,
            person,
            answers,
            channel=Channel.PAPER,
            entered_by=request.user,
            scan=request.FILES.get("scan"),
            cast_at=cast_at,
            replace=bool(p.get("replace")),
        )
    except (ValidationError, ClosedVoteError, PermissionDenied) as exc:
        _error(request, exc)
    else:
        messages.success(
            request, _("Paper ballot for unit %(unit)s recorded.") % {"unit": unit.number}
        )
    return redirect("votes:detail", pk=vote.pk)
