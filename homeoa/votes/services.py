"""Owner vote services (GOV-07). The only supported way to create, open, cast in and close
a vote.

* ``create_vote(...)`` / ``open_vote(vote)`` / ``close_vote(vote)``.
* ``cast_ballot(vote, unit, person, answers, ...)``: one active ballot per unit. A ballot
  by the same owner replaces their earlier one; a different joint owner's ballot is
  refused unless ``replace=True`` (first wins unless replaced). Every replacement is
  audited. Paper ballots are entered by the board with the scan attached.
* ``tally(vote)``: Decimal totals per choice, quorum and threshold per question.
* ``ballot_rows(vote)``: who voted, and how unless the vote is secret.

Thresholds: "majority" means more than 50% (of votes cast, or of all votes). A rule-pack
threshold reads ``votes.<kind>.threshold_percent`` (or its renamed key) on the close date
and is recorded with its verified flag; when the pack leaves it unset the generic default
is a majority of all votes. Quorum from the pack reads ``meetings.owner.quorum_percent``;
unset means no quorum. Abstentions count toward quorum, not as votes cast.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import audit, rules
from homeoa.registry import services as registry
from homeoa.registry.models import Unit
from homeoa.rules.keys import comparison_key

from .models import (
    Ballot,
    BallotAnswer,
    Channel,
    Choice,
    ClosedVoteError,
    Comparison,
    Question,
    Quorum,
    Threshold,
    Vote,
    VoteStatus,
    Weighting,
)

HUNDRED = Decimal("100")
MAJORITY = Decimal("50")  # "majority" = more than half; a definition, not a statute value
SCAN_FOLDER = "votes/paper"
QUORUM_KEY = "meetings.owner.quorum_percent"

# Rule-pack comparison values -> the vote's Comparison.
_PACK_COMPARISON = {"gte": Comparison.AT_LEAST, "gt": Comparison.MORE_THAN}


class BallotError(ValidationError):
    pass


class BallotExistsError(BallotError):
    """Another joint owner already voted for this unit; pass ``replace=True`` to replace."""


# ---------------------------------------------------------------- setup


@transaction.atomic
def create_vote(
    association,
    *,
    title: str,
    kind: str,
    opens_at: datetime.datetime,
    closes_at: datetime.datetime,
    questions: list[tuple[str, list]],
    weighting: str = "",
    threshold: str = Threshold.MAJORITY_CAST,
    pack_kind: str = "",
    comparison: str = "",
    quorum: str = Quorum.NONE,
    quorum_percent: Decimal | None = None,
    secret: bool = False,
    description: str = "",
    user=None,
) -> Vote:
    """``questions`` is ``[(text, [label | (label, {"affirmative": .., "abstain": ..})])]``.
    ``weighting`` defaults to the association setting (pilot: one vote per unit)."""
    weighting = weighting or registry.vote_weighting(association)
    if threshold != Threshold.RULE_PACK:  # rule pack: blank reads the pack at close
        pack_kind = ""
        comparison = comparison or Comparison.MORE_THAN
    vote = Vote(
        association=association,
        title=title,
        description=description,
        kind=kind,
        opens_at=opens_at,
        closes_at=closes_at,
        weighting=weighting,
        threshold=threshold,
        pack_kind=pack_kind,
        comparison=comparison,
        quorum=quorum,
        quorum_percent=quorum_percent,
        secret=secret,
        created_by=user,
    )
    vote.full_clean(exclude=["created_by"])
    vote.save()
    for qpos, (text, choices) in enumerate(questions):
        question = Question.objects.create(
            association=association, vote=vote, text=text, position=qpos
        )
        for cpos, spec in enumerate(choices):
            label, flags = (spec, {}) if isinstance(spec, str) else spec
            Choice.objects.create(
                association=association,
                question=question,
                label=label,
                position=cpos,
                affirmative=flags.get("affirmative", False),
                abstain=flags.get("abstain", False),
            )
    return vote


def yes_no_abstain() -> list:
    return [
        (_("Yes"), {"affirmative": True}),
        _("No"),
        (_("Abstain"), {"abstain": True}),
    ]


def open_vote(vote: Vote) -> Vote:
    if vote.status != VoteStatus.DRAFT:
        raise ValidationError(_("Only a draft vote can be opened."))
    questions = list(vote.questions.prefetch_related("choices"))
    if not questions or any(q.choices.count() < 2 for q in questions):
        raise ValidationError(_("Every question needs at least two choices."))
    # A queryset update skips the generic "update" audit row; opening is its own event.
    Vote.all_associations.filter(pk=vote.pk).update(status=VoteStatus.OPEN)
    vote.status = VoteStatus.OPEN
    audit.record("votes.opened", vote)
    return vote


# ---------------------------------------------------------------- ballots


def _check_open(vote: Vote, at: datetime.datetime, *, paper: bool, now: datetime.datetime):
    if vote.status != VoteStatus.OPEN:
        raise ClosedVoteError(_("This vote is not open."))
    if at < vote.opens_at or at > vote.closes_at:
        raise ClosedVoteError(_("Ballots are accepted only while the vote is open."))
    if not paper and now > vote.closes_at:
        raise ClosedVoteError(_("Ballots are accepted only while the vote is open."))


def _resolve_answers(vote: Vote, answers: dict) -> list[tuple[Question, Choice]]:
    pairs = []
    questions = {q.pk: q for q in vote.questions.all()}
    for q, c in answers.items():
        q_pk = getattr(q, "pk", q)
        question = questions.get(q_pk)
        if question is None:
            raise BallotError(_("That question is not part of this vote."))
        choice = question.choices.filter(pk=getattr(c, "pk", c)).first()
        if choice is None:
            raise BallotError(_("That choice does not belong to the question."))
        pairs.append((question, choice))
    if not pairs:
        raise BallotError(_("A ballot needs at least one answer."))
    return pairs


@transaction.atomic
def cast_ballot(
    vote: Vote,
    unit: Unit,
    person,
    answers: dict,
    *,
    channel: str = Channel.ELECTRONIC,
    entered_by=None,
    scan=None,
    cast_at: datetime.datetime | None = None,
    replace: bool = False,
    now: datetime.datetime | None = None,
) -> Ballot:
    """Record ``unit``'s ballot cast by owner ``person``.

    Paper and in-person ballots are entered by a board user (``entered_by``, who needs
    ``votes.administer``) with ``cast_at`` the time received; a paper ballot needs its
    ``scan``. Electronic ballots are cast now.
    """
    vote = Vote.all_associations.select_for_update().get(pk=vote.pk)
    now = now or timezone.now()
    paper = channel != Channel.ELECTRONIC
    if paper:
        _require_admin(entered_by, vote.association)
        if channel == Channel.PAPER and scan is None:
            raise BallotError(_("Attach the scan of the paper ballot."))
        cast_at = cast_at or now
    else:
        cast_at = now
    _check_open(vote, cast_at, paper=paper, now=now)
    if unit.association_id != vote.association_id or person.association_id != (vote.association_id):
        raise BallotError(_("That unit or owner is not in this association."))
    on = timezone.localdate(cast_at, timezone=_tz(vote.association))
    if person not in registry.current_owners(unit, on):
        raise PermissionDenied(_("Only an owner of the unit can vote for it."))
    pairs = _resolve_answers(vote, answers)

    previous = Ballot.all_associations.filter(
        vote=vote, unit=unit, superseded_by__isnull=True
    ).first()
    if previous is not None and previous.person_id != person.pk and not replace:
        raise BallotExistsError(
            _("Another owner of this unit has already voted. Replace that ballot?")
        )

    ballot = Ballot(
        association=vote.association,
        vote=vote,
        unit=unit,
        person=person,
        channel=channel,
        cast_at=cast_at,
        entered_by=entered_by if paper else None,
    )
    if previous is not None:
        _supersede(previous, ballot)
    else:
        ballot.save()
    for question, choice in pairs:
        BallotAnswer.objects.create(
            association=vote.association, ballot=ballot, question=question, choice=choice
        )
    if scan is not None:
        _store_scan(ballot, scan)
    if previous is not None:
        audit.record(
            "votes.ballot_replaced",
            ballot,
            message=_("Ballot for unit %(unit)s replaced (earlier ballot #%(pk)s).")
            % {"unit": unit.number, "pk": previous.pk},
        )
    else:
        audit.record("votes.ballot_cast", ballot)
    return ballot


def _supersede(previous: Ballot, ballot: Ballot) -> None:
    """Save ``ballot`` and point ``previous`` at it. The partial unique index allows one
    active ballot per unit, so ``previous`` is first marked superseded by itself."""
    Ballot.all_associations.filter(pk=previous.pk).update(superseded_by=previous.pk)
    ballot.save()
    previous.superseded_by = ballot
    previous.save(update_fields=["superseded_by"])


def _store_scan(ballot: Ballot, upload) -> None:
    from homeoa.providers import get_provider

    storage = get_provider("FileStorage", ballot.association)
    name = getattr(upload, "name", "") or "ballot.pdf"
    stored = storage.put(
        ballot.association_id,
        name,
        upload,
        content_type=getattr(upload, "content_type", "") or "application/octet-stream",
        folder=SCAN_FOLDER,
    )
    ballot.scan_file_key, ballot.scan_file_name = stored.key, name[:200]
    ballot.save(update_fields=["scan_file_key", "scan_file_name"])


def _tz(association):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(association.timezone)
    except Exception:  # noqa: BLE001 - bad zone name falls back to UTC
        return datetime.UTC


def _require_admin(user, association) -> None:
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm

    if user is None or not has_perm(user, perms.VOTES_ADMINISTER, association=association):
        raise PermissionDenied(_("Only the board can enter paper or in-person ballots."))


# ---------------------------------------------------------------- tally


@dataclasses.dataclass(frozen=True)
class Rule:
    percent: Decimal | None
    comparison: str
    source: str  # "majority", "default", "override" or a pack id
    verified: bool = False
    citation: str = ""

    def met(self, value: Decimal, base: Decimal) -> bool:
        if self.percent is None:
            return True
        if base <= 0:
            return False
        lhs, rhs = value * HUNDRED, self.percent * base
        return lhs > rhs if self.comparison == Comparison.MORE_THAN else lhs >= rhs


@dataclasses.dataclass(frozen=True)
class QuestionResult:
    question: Question
    totals: dict[int, Decimal]  # choice pk -> weight
    cast: Decimal  # weight on non-abstain choices
    answered: Decimal  # weight of units that answered, abstentions included
    affirmative: Decimal | None
    passed: bool | None  # None when the question has no affirmative choice


@dataclasses.dataclass(frozen=True)
class Tally:
    weights: dict[int, Decimal]  # unit pk -> weight
    total: Decimal
    participating: Decimal
    quorum_rule: Rule
    quorum_met: bool
    threshold_rule: Rule
    questions: list[QuestionResult]


def unit_weight(vote: Vote, unit: Unit) -> Decimal:
    if vote.weighting == Weighting.ALLOCATED_INTEREST:
        return unit.allocated_interest
    if vote.weighting == Weighting.CUSTOM:
        return unit.vote_weight
    return Decimal("1")


def _as_of(vote: Vote) -> datetime.date:
    return timezone.localdate(vote.closes_at, timezone=_tz(vote.association))


def threshold_rule(vote: Vote) -> Rule:
    if vote.threshold != Threshold.RULE_PACK:
        return Rule(MAJORITY, Comparison.MORE_THAN, "majority")
    key = f"votes.{vote.pack_kind}.threshold_percent"
    resolved = rules.resolve(vote.association, key, _as_of(vote))
    if resolved.value is None:
        return Rule(MAJORITY, Comparison.MORE_THAN, "default")
    comparison = vote.comparison
    if not comparison:
        pack_cmp = rules.resolve(vote.association, comparison_key(key), _as_of(vote)).value
        comparison = _PACK_COMPARISON.get(pack_cmp, Comparison.MORE_THAN)  # majority
    return Rule(
        Decimal(resolved.value),
        comparison,
        resolved.source,
        resolved.verified,
        resolved.citation,
    )


def quorum_rule(vote: Vote) -> Rule:
    if vote.quorum == Quorum.CUSTOM:
        return Rule(vote.quorum_percent, Comparison.AT_LEAST, "vote")
    if vote.quorum == Quorum.RULE_PACK:
        resolved = rules.resolve(vote.association, QUORUM_KEY, _as_of(vote))
        if resolved.value is None:
            return Rule(None, Comparison.AT_LEAST, "default")
        return Rule(
            Decimal(resolved.value),
            Comparison.AT_LEAST,
            resolved.source,
            resolved.verified,
            resolved.citation,
        )
    return Rule(None, Comparison.AT_LEAST, "none")


def _active_ballots(vote: Vote):
    return (
        Ballot.all_associations.filter(vote=vote, superseded_by__isnull=True)
        .select_related("unit")
        .prefetch_related("answers__choice")
        .order_by("unit_id")
    )


def tally(vote: Vote) -> Tally:
    units = Unit.all_associations.filter(association=vote.association)
    weights = {u.pk: unit_weight(vote, u) for u in units}
    total = sum(weights.values(), Decimal("0"))
    ballots = list(_active_ballots(vote))
    participating = sum((weights.get(b.unit_id, Decimal("0")) for b in ballots), Decimal("0"))
    q_rule = quorum_rule(vote)
    quorum_met = q_rule.met(participating, total)
    t_rule = threshold_rule(vote)

    results = []
    for question in vote.questions.prefetch_related("choices"):
        choices = list(question.choices.all())
        totals = {c.pk: Decimal("0") for c in choices}
        abstain_pks = {c.pk for c in choices if c.abstain}
        cast = answered = Decimal("0")
        for b in ballots:
            for a in b.answers.all():
                if a.question_id != question.pk:
                    continue
                w = weights.get(b.unit_id, Decimal("0"))
                totals[a.choice_id] += w
                answered += w
                if a.choice_id not in abstain_pks:
                    cast += w
        yes = next((c for c in choices if c.affirmative), None)
        if yes is None:
            affirmative, passed = None, None
        else:
            affirmative = totals[yes.pk]
            base = total if vote.threshold != Threshold.MAJORITY_CAST else cast
            passed = quorum_met and t_rule.met(affirmative, base)
        results.append(QuestionResult(question, totals, cast, answered, affirmative, passed))
    return Tally(weights, total, participating, q_rule, quorum_met, t_rule, results)


def ballots_hash(vote: Vote) -> str:
    """SHA-256 over the active ballots (who, channel, when, answers) in a fixed order."""
    lines = []
    for b in _active_ballots(vote):
        answers = ",".join(
            f"{a.question_id}:{a.choice_id}"
            for a in sorted(b.answers.all(), key=lambda a: a.question_id)
        )
        lines.append(
            f"{b.pk}|{b.unit_id}|{b.person_id}|{b.channel}|{b.cast_at.isoformat()}|{answers}"
        )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _rule_json(rule: Rule) -> dict:
    return {
        "percent": None if rule.percent is None else str(rule.percent),
        "comparison": rule.comparison,
        "source": rule.source,
        "verified": rule.verified,
        "citation": rule.citation,
    }


def snapshot(vote: Vote, t: Tally) -> dict:
    units = {u.pk: u.number for u in Unit.all_associations.filter(pk__in=t.weights)}
    return {
        "weighting": vote.weighting,
        "threshold": vote.threshold,
        "pack_kind": vote.pack_kind,
        "eligible": [
            {"unit": pk, "number": units[pk], "weight": str(w)}
            for pk, w in sorted(t.weights.items())
        ],
        "total": str(t.total),
        "participating": str(t.participating),
        "quorum": {**_rule_json(t.quorum_rule), "met": t.quorum_met},
        "threshold_rule": _rule_json(t.threshold_rule),
        "questions": [
            {
                "question": r.question.pk,
                "text": r.question.text,
                "totals": {str(pk): str(w) for pk, w in r.totals.items()},
                "cast": str(r.cast),
                "answered": str(r.answered),
                "affirmative": None if r.affirmative is None else str(r.affirmative),
                "passed": r.passed,
            }
            for r in t.questions
        ],
    }


@transaction.atomic
def close_vote(vote: Vote, *, user=None, now: datetime.datetime | None = None) -> Vote:
    """Close the vote and record the result with its snapshot and ballots hash."""
    vote = Vote.all_associations.select_for_update().get(pk=vote.pk)
    if vote.status != VoteStatus.OPEN:
        raise ClosedVoteError(_("Only an open vote can be closed."))
    if user is not None:
        _require_admin(user, vote.association)
    t = tally(vote)
    vote.status = VoteStatus.CLOSED
    vote.closed_at = now or timezone.now()
    vote.result = snapshot(vote, t)
    vote.ballots_hash = ballots_hash(vote)
    vote.save(update_fields=["status", "closed_at", "result", "ballots_hash"])
    audit.record("votes.vote_closed", vote, message=vote.ballots_hash)
    return vote


def verify(vote: Vote) -> bool:
    """True if the stored ballots still hash to what was recorded at close."""
    return vote.status == VoteStatus.CLOSED and ballots_hash(vote) == vote.ballots_hash


# ---------------------------------------------------------------- display


@dataclasses.dataclass(frozen=True)
class BallotRow:
    ballot: Ballot
    answers: dict[int, Choice] | None  # question pk -> choice; None when secret


def ballot_rows(vote: Vote, *, include_superseded: bool = False) -> list[BallotRow]:
    """Who voted (unit, owner, channel, time); how they voted only for non-secret votes."""
    qs = Ballot.all_associations.filter(vote=vote).select_related("unit", "person")
    if not include_superseded:
        qs = qs.filter(superseded_by__isnull=True)
    rows = []
    for b in qs.prefetch_related("answers__choice"):
        answers = None if vote.secret else {a.question_id: a.choice for a in b.answers.all()}
        rows.append(BallotRow(b, answers))
    return rows
