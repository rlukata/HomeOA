"""Non-binding owner polls (GOV-11).

The board asks a question with two or more choices for a set period. Each unit answers
once (and may change its answer while the poll is open): its owners, or also its tenants
when the board says so. Results go to the board, and to residents after the poll closes
when the board chooses. A poll is never a vote: nothing is decided by it.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from .models import Poll, PollAnswer

MAX_CHOICES = 8


def parse_choices(text: str) -> list[str]:
    choices = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(choices) < 2:
        raise ValidationError(_("Give at least two choices, one per line."))
    if len(choices) > MAX_CHOICES:
        raise ValidationError(_("Give at most %(n)s choices.") % {"n": MAX_CHOICES})
    return choices


@transaction.atomic
def create(association, *, question, details, choices, opens_on, closes_on, include_tenants,
           show_results, user=None) -> Poll:  # fmt: skip
    if closes_on < opens_on:
        raise ValidationError(_("The poll must close on or after the day it opens."))
    return Poll.all_associations.create(
        association=association,
        question=question.strip(),
        details=details.strip(),
        choices=choices,
        opens_on=opens_on,
        closes_on=closes_on,
        include_tenants=include_tenants,
        show_results=show_results,
        created_by=user,
    )


def close_now(poll: Poll, on: dt.date) -> Poll:
    poll.closed_early_on = on
    poll.save(update_fields=["closed_early_on"])
    return poll


def units_for(user, poll: Poll, on: dt.date) -> list:
    """The units ``user`` answers for in ``poll``: owned, and lived in if tenants answer."""
    from homeoa.access.services import units_for as user_units
    from homeoa.portal.views.home import is_owner_of

    units = user_units(user, poll.association)
    if poll.include_tenants:
        return sorted(units, key=lambda u: u.sort_key)
    return sorted((u for u in units if is_owner_of(user, u, on)), key=lambda u: u.sort_key)


def open_polls(user, association, on: dt.date) -> list[tuple[Poll, list]]:
    """Open polls the user can answer, each with its ``[(unit, answer or None)]``."""
    out = []
    for poll in Poll.all_associations.filter(
        association=association, opens_on__lte=on, closes_on__gte=on, closed_early_on__isnull=True
    ):
        units = units_for(user, poll, on)
        if units:
            answers = {a.unit_id: a for a in poll.answers.filter(unit__in=units)}
            out.append((poll, [(u, answers.get(u.pk)) for u in units]))
    return out


@transaction.atomic
def answer(poll: Poll, *, user, person, unit, choice: int, on: dt.date) -> PollAnswer:
    if not poll.is_open(on):
        raise ValidationError(_("This poll is closed."))
    if unit not in units_for(user, poll, on):
        raise ValidationError(_("You can't answer for this unit."))
    if not 0 <= choice < len(poll.choices):
        raise ValidationError(_("Pick one of the choices."))
    obj, _created = PollAnswer.all_associations.update_or_create(
        association=poll.association,
        poll=poll,
        unit=unit,
        defaults={"person": person, "choice": choice},
    )
    return obj


def results(poll: Poll) -> dict:
    """``{"rows": [(choice, count, pct)], "answered": n, "units": total}``."""
    from homeoa.registry.models import Unit

    counts = [0] * len(poll.choices)
    for a in poll.answers.all():
        if a.choice < len(counts):
            counts[a.choice] += 1
    answered = sum(counts)
    rows = [
        (c, n, round(n * 100 / answered) if answered else 0)
        for c, n in zip(poll.choices, counts, strict=True)
    ]
    units = Unit.all_associations.filter(association=poll.association).count()
    return {"rows": rows, "answered": answered, "units": units}
