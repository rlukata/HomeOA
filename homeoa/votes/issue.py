"""Issuing ballots when a vote opens.

For each unit, owners with e-delivery consent get an email with a link to the portal ballot;
when any current owner of a unit lacks consent, the unit also gets a paper ballot letter
(the board-editable "owner-ballot" letter template, with the questions as a table). The
letters go into one draft mail batch the board sends from Letters. Both run after the
opening commits; a failure is logged and audited, never raised.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.notify import channels as ch
from homeoa.notify import services as nsvc
from homeoa.notify import types as ntypes
from homeoa.registry import services as registry
from homeoa.registry.models import Unit

from .models import Vote

logger = logging.getLogger(__name__)

NOTIFICATION_TYPE = "votes.ballot"
TEMPLATE_KEY = "owner-ballot"

TEMPLATE = {
    "name": gettext_lazy("Owner ballot"),
    "subject": gettext_lazy("Ballot: {{ vote.title }}"),
    "body": gettext_lazy(
        "Dear {{ owner.name }},\n\n"
        "The board of {{ association.name }} has opened a vote: {{ vote.title }}. "
        "This is the ballot for unit {{ unit.number }}.\n\n"
        "Mark one choice for each question below, sign and date the ballot, and return it "
        "to the board so that it arrives by {{ vote.closes }}. You may also vote online in "
        "the owner portal.\n\n"
        "Signature: ______________________   Date: ____________\n\n"
        "Sincerely,\nThe Board of Directors\n{{ association.name }}"
    ),
}


def register() -> None:
    """Notification type and the ``vote.*`` letter merge fields (called from ``ready``)."""
    from homeoa.letters import merge

    ntypes.register(
        ntypes.NotificationType(
            key=NOTIFICATION_TYPE,
            label=gettext_lazy("Owner vote ballot"),
            default_channels=(ch.EMAIL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )
    merge.register(
        "vote.title",
        gettext_lazy("Vote title"),
        lambda ctx: ctx.extra.get("vote_title", ""),
        sample="Waive the 2026 audit",
    )
    merge.register(
        "vote.closes",
        gettext_lazy("Vote closes"),
        lambda ctx: ctx.extra.get("vote_closes", ""),
        sample="November 30, 2026",
    )


def ballot_template(association):
    from homeoa.letters import services as letters
    from homeoa.letters.models import LetterTemplate

    template = LetterTemplate.all_associations.filter(
        association=association, key=TEMPLATE_KEY
    ).first()
    if template is None:
        template = letters.create_template(
            association,
            key=TEMPLATE_KEY,
            name=str(TEMPLATE["name"]),
            subject=str(TEMPLATE["subject"]),
            body=str(TEMPLATE["body"]),
            draft_wording=True,
        )
    return template


def closes_text(vote: Vote) -> str:
    local = timezone.localtime(vote.closes_at, _tz(vote))
    return date_format(local, "F j, Y")


def _tz(vote):
    from .services import _tz as tz

    return tz(vote.association)


def ballot_table(vote: Vote) -> list[dict]:
    tables = []
    for q in vote.questions.prefetch_related("choices"):
        rows = [[_("Mark one"), _("Choice")]]
        rows += [["[   ]", c.label] for c in q.choices.all()]
        tables.append({"heading": q.text, "rows": rows})
    return tables


def route(vote: Vote) -> list[tuple[Unit, list, list]]:
    """``(unit, email_owners, paper)`` per unit: the owners who get the emailed link, and
    the owners without e-delivery consent (the unit then gets a paper ballot)."""
    ntype = ntypes.get(NOTIFICATION_TYPE)
    on = timezone.localdate(vote.opens_at, _tz(vote))
    today = timezone.localdate(timezone.now(), _tz(vote))
    on = max(on, today)
    out = []
    units = sorted(Unit.all_associations.filter(association=vote.association), key=_sort)
    for unit in units:
        owners = list(registry.current_owners(unit, on))
        email = [p for p in owners if ch.EMAIL in nsvc.channels_for(ntype, p)]
        paper = [p for p in owners if p not in email]
        out.append((unit, email, paper))
    return out


def _sort(unit):
    return getattr(unit, "sort_key", unit.number)


def issue_ballots(vote: Vote, *, user=None) -> None:
    """Queue the emailed links and the paper ballot letters for after commit."""
    plan = route(vote)
    link = (getattr(settings, "HOMEOA_BASE_URL", "") or "") + reverse(
        "votes_portal:vote", args=[vote.pk]
    )
    closes = closes_text(vote)
    for unit, email, _paper in plan:
        if not email:
            continue
        nsvc.notify_after_commit(
            NOTIFICATION_TYPE,
            email,
            {
                "subject": _("Ballot: %(title)s") % {"title": vote.title},
                "body": _(
                    "%(association)s has opened a vote: %(title)s. Vote for unit %(unit)s "
                    "in the owner portal before %(closes)s:\n\n%(link)s"
                )
                % {
                    "association": vote.association.name,
                    "title": vote.title,
                    "unit": unit.number,
                    "closes": closes,
                    "link": link,
                },
            },
            obj=vote,
            period=f"unit:{unit.pk}",
            association=vote.association,
            unit=unit,
        )
    paper_units = [unit for unit, _email, paper in plan if paper]
    if paper_units:
        transaction.on_commit(lambda: _mail_letters(vote.pk, [u.pk for u in paper_units], user))


def _mail_letters(vote_pk: int, unit_pks: list[int], user) -> None:
    vote = Vote.all_associations.select_related("association").get(pk=vote_pk)
    try:
        with transaction.atomic():
            make_letters(vote, Unit.all_associations.filter(pk__in=unit_pks), user=user)
    except Exception as exc:  # noqa: BLE001 - alerts never raise
        logger.exception("ballot letters for vote %s failed", vote_pk)
        audit.record("votes.ballot_letters_failed", vote, message=str(exc)[:500])


def make_letters(vote: Vote, units, *, user=None):
    from homeoa.letters import batches
    from homeoa.letters import services as letters

    template = ballot_template(vote.association)
    tables = ballot_table(vote)
    made = [
        letters.generate_letter(
            template,
            unit,
            user=user,
            tables=tables,
            vote_title=vote.title,
            vote_closes=closes_text(vote),
        )
        for unit in sorted(units, key=_sort)
    ]
    batch = batches.create_batch(
        vote.association,
        made,
        name=_("Ballots: %(title)s") % {"title": vote.title},
        user=user,
    )
    audit.record(
        "votes.ballot_letters",
        vote,
        changes={"letters": [letter.pk for letter in made], "batch": batch.pk},
        actor=user,
    )
    return batch
