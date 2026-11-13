"""Owner portal pages for votes (``/me/votes/``).

Owners only (``votes.vote``, tenants have no such permission), and only once residents
are on (``resident_view``). An owner votes for each unit they own today.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.decorators import resident_view
from homeoa.portal.views.base import _render
from homeoa.registry import services as registry
from homeoa.registry.models import Person

from . import services
from .models import Ballot, ClosedVoteError, Vote, VoteStatus
from .views import receipt_id


def _person(request):
    person = Person.objects.filter(user=request.user).first()
    if person is None:
        raise PermissionDenied
    return person


def _my_units(request, person):
    today = timezone.localdate(timezone.now(), services._tz(request.association))
    return registry.units_owned_by(person, today)


def _active(vote, unit):
    return Ballot.all_associations.filter(vote=vote, unit=unit, superseded_by__isnull=True).first()


@resident_view(perm=perms.VOTES_VOTE)
def index(request):
    person = _person(request)
    units = _my_units(request, person)
    rows = []
    for vote in Vote.objects.filter(status__in=[VoteStatus.OPEN, VoteStatus.CLOSED]):
        if vote.status == VoteStatus.CLOSED and not vote.result:
            continue
        rows.append(
            {
                "vote": vote,
                "units": [{"unit": u, "ballot": _active(vote, u)} for u in units],
            }
        )
    return _render(
        request,
        "more",
        "votes/portal_index.html",
        open_rows=[r for r in rows if r["vote"].status == VoteStatus.OPEN],
        closed_rows=[r for r in rows if r["vote"].status == VoteStatus.CLOSED],
    )


@resident_view(perm=perms.VOTES_VOTE)
def vote(request, pk):
    vote = get_object_or_404(Vote, pk=pk, status__in=[VoteStatus.OPEN, VoteStatus.CLOSED])
    person = _person(request)
    units = _my_units(request, person)
    if not units:
        raise PermissionDenied
    questions = list(vote.questions.prefetch_related("choices"))
    unit = units[0]
    if request.GET.get("unit") or request.POST.get("unit"):
        wanted = request.POST.get("unit") or request.GET.get("unit")
        unit = next((u for u in units if str(u.pk) == wanted), None)
        if unit is None:
            raise Http404
    ask_replace = False
    if request.method == "POST":
        answers = {}
        for q in questions:
            value = request.POST.get(f"q{q.pk}", "")
            if value.isdigit():
                answers[q.pk] = int(value)
        if len(answers) != len(questions):
            messages.error(request, _("Answer every question."))
        else:
            try:
                ballot = services.cast_ballot(
                    vote, unit, person, answers, replace=bool(request.POST.get("replace"))
                )
            except services.BallotExistsError as exc:
                _error(request, exc)
                ask_replace = True
            except (ValidationError, ClosedVoteError, PermissionDenied) as exc:
                _error(request, exc)
            else:
                return redirect("votes_portal:receipt", pk=vote.pk, ballot=ballot.pk)
    current = _active(vote, unit)
    mine = set()
    if current is not None and current.person_id == person.pk and not vote.secret:
        mine = {a.choice_id for a in current.answers.all()}
    return _render(
        request,
        "more",
        "votes/portal_vote.html",
        vote=vote,
        questions=questions,
        my_units=units,
        unit=unit,
        current=current,
        mine=mine,
        ask_replace=ask_replace,
        is_open=vote.status == VoteStatus.OPEN and timezone.now() <= vote.closes_at,
    )


@resident_view(perm=perms.VOTES_VOTE)
def receipt(request, pk, ballot):
    person = _person(request)
    ballot = get_object_or_404(Ballot, pk=ballot, vote_id=pk, person=person)
    return _render(
        request,
        "more",
        "votes/portal_receipt.html",
        vote=ballot.vote,
        ballot=ballot,
        receipt=receipt_id(ballot),
    )


def _error(request, exc) -> None:
    if isinstance(exc, ValidationError):
        messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, str(exc))
