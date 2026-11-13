"""Annual meeting workflow (GOV-03, GOV-07, FIN-13).

One checklist per annual owners meeting:

1. The board adopts next year's budget.
2. The budget summary goes to owners within ``budget.distribute_within_days`` of adoption.
3. The ratification meeting falls ``budget.ratification.min_days`` to ``max_days`` after
   the summary was sent.
4. Notice of the meeting goes out in the rule pack's notice window.

Every number comes from the rule pack (shown with its verified flag); an unset key means
no deadline. The ratification vote passes its "reject" choice only by the pack's rejection
threshold of all votes (``votes.budget_rejection``), with no quorum unless the pack says
``budget.ratification.quorum_required``; otherwise the budget is ratified. The audit-waiver
vote uses the pack's ``votes.audit_waiver`` threshold and, when it passes, closes the
year's audit obligation period on its waiver path with the vote as evidence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa import audit, rules
from homeoa.core.context import association_today

from . import services
from .models import AnnualWorkflow, Meeting

DISTRIBUTE_KEY = "budget.distribute_within_days"
RATIFY_MIN_KEY = "budget.ratification.min_days"
RATIFY_MAX_KEY = "budget.ratification.max_days"
QUORUM_REQUIRED_KEY = "budget.ratification.quorum_required"
WAIVER_KEY = "votes.audit_waiver.threshold_percent"
AUDIT_OBLIGATION = "annual_audit"


class AnnualError(Exception):
    pass


# ---------------------------------------------------------------- setup


def default_budget(meeting: Meeting):
    """Next fiscal year's budget: the adopted one, else the newest draft."""
    from homeoa.budget.models import Budget, BudgetStatus

    qs = Budget.all_associations.filter(
        association_id=meeting.association_id, year=meeting.date.year + 1
    )
    return (
        qs.filter(status=BudgetStatus.ADOPTED).first()
        or qs.filter(status=BudgetStatus.DRAFT).order_by("-pk").first()
    )


def workflow_for(meeting: Meeting) -> AnnualWorkflow:
    if meeting.kind != Meeting.Kind.ANNUAL:
        raise AnnualError(_("Only an annual owners meeting has this workflow."))
    wf = AnnualWorkflow.all_associations.filter(meeting=meeting).first()
    if wf is None:
        wf = AnnualWorkflow.objects.create(
            association_id=meeting.association_id, meeting=meeting, budget=default_budget(meeting)
        )
    return wf


def set_budget(wf: AnnualWorkflow, budget) -> AnnualWorkflow:
    if budget.association_id != wf.association_id:
        raise AnnualError("budget from another association")
    if wf.ratification_vote_id:
        raise AnnualError(_("The ratification vote exists; the budget can no longer change."))
    wf.budget = budget
    wf.budget_sent_on = None
    wf.save(update_fields=["budget", "budget_sent_on"])
    return wf


def _require_adopted(wf: AnnualWorkflow) -> None:
    if wf.budget is None or not wf.budget.is_adopted:
        raise AnnualError(
            _("The board has not adopted next year's budget yet. Adopt it in Budget first.")
        )


def mark_budget_sent(wf: AnnualWorkflow, on: dt.date, *, user=None) -> AnnualWorkflow:
    _require_adopted(wf)
    wf.budget_sent_on = on
    wf.save(update_fields=["budget_sent_on"])
    audit.record("meetings.budget_summary_sent", wf, changes={"on": on.isoformat()}, actor=user)
    return wf


# ---------------------------------------------------------------- steps


@dataclass
class Step:
    key: str
    label: str
    due: dt.date | None = None
    done: bool = False
    done_on: dt.date | None = None
    problem: str = ""
    rules: list = field(default_factory=list)  # rules.Resolved shown with a verified badge
    window: tuple[dt.date | None, dt.date | None] | None = None


def _days(r) -> int | None:
    return r.value if r.is_set else None


def _plus(day: dt.date | None, days: int | None) -> dt.date | None:
    return None if day is None or days is None else day + dt.timedelta(days=days)


def _fmt(day: dt.date) -> str:
    return date_format(day, "M j, Y")


def adopted_on(wf: AnnualWorkflow) -> dt.date | None:
    b = wf.budget
    if b is None or not b.is_adopted or b.adopted_at is None:
        return None
    return timezone.localdate(b.adopted_at, timezone=_tz(wf.association))


def _tz(association):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(association.timezone)
    except Exception:  # noqa: BLE001 - bad zone name falls back to UTC
        return dt.UTC


def summary_deadline(wf: AnnualWorkflow, as_of: dt.date | None = None) -> dt.date | None:
    """Last day to send the summary: within N days of adoption, and early enough that the
    meeting is at least the minimum number of days after it."""
    a, meeting = wf.association, wf.meeting
    within = _days(rules.resolve(a, DISTRIBUTE_KEY, as_of))
    min_days = _days(rules.resolve(a, RATIFY_MIN_KEY, as_of))
    candidates = [
        d
        for d in (
            _plus(adopted_on(wf), within),
            meeting.date - dt.timedelta(days=min_days) if min_days is not None else None,
        )
        if d is not None
    ]
    return min(candidates) if candidates else None


def ratification_window(wf: AnnualWorkflow, as_of: dt.date | None = None):
    """``(earliest, latest)`` meeting dates counted from the day the summary was sent."""
    a = wf.association
    sent = wf.budget_sent_on
    return (
        _plus(sent, _days(rules.resolve(a, RATIFY_MIN_KEY, as_of))),
        _plus(sent, _days(rules.resolve(a, RATIFY_MAX_KEY, as_of))),
    )


def steps(wf: AnnualWorkflow, today: dt.date | None = None) -> list[Step]:
    a, meeting = wf.association, wf.meeting
    today = today or association_today(a)
    budget = wf.budget

    adopt = Step("adopt", _("Board adopts next year's budget"))
    adopt.done = budget is not None and budget.is_adopted
    adopt.done_on = adopted_on(wf)
    if budget is None:
        adopt.problem = _("No budget for %(year)s yet. Create one in Budget.") % {
            "year": meeting.date.year + 1
        }
    elif not adopt.done:
        adopt.problem = _("%(budget)s is still a draft.") % {"budget": budget}

    send = Step("send", _("Send the budget summary to owners"))
    send.rules = [rules.resolve(a, DISTRIBUTE_KEY, today), rules.resolve(a, RATIFY_MIN_KEY, today)]
    send.due = summary_deadline(wf, today)
    send.done = wf.budget_sent_on is not None
    send.done_on = wf.budget_sent_on
    if not adopt.done:
        send.problem = _("Adopt the budget first: the summary goes out after adoption.")

    meet = Step("meeting", _("Hold the ratification meeting"))
    meet.rules = [rules.resolve(a, RATIFY_MIN_KEY, today), rules.resolve(a, RATIFY_MAX_KEY, today)]
    meet.due = meeting.date
    meet.done = meeting.status == Meeting.Status.HELD
    earliest, latest = ratification_window(wf, today)
    meet.window = (earliest, latest)
    if earliest is not None and meeting.date < earliest:
        meet.problem = _("The meeting is too soon after the summary: hold it from %(date)s.") % {
            "date": _fmt(earliest)
        }
    elif latest is not None and meeting.date > latest:
        meet.problem = _("The meeting is too late after the summary: hold it by %(date)s.") % {
            "date": _fmt(latest)
        }

    notice = Step("notice", _("Send the meeting notice"))
    window = services.notice_window(meeting, today)
    notice.rules = [r for r in (window.min_rule, window.max_rule) if r is not None]
    notice.due = window.latest
    notice.done = meeting.noticed_on is not None
    notice.done_on = meeting.noticed_on
    return [adopt, send, meet, notice]


# ---------------------------------------------------------------- votes


def _vote_times(meeting: Meeting) -> tuple[dt.datetime, dt.datetime]:
    tz = _tz(meeting.association)
    closes = dt.datetime.combine(meeting.date, dt.time(23, 59), tzinfo=tz)
    opens = min(timezone.now(), closes - dt.timedelta(days=1))
    return opens, closes


@transaction.atomic
def create_votes(wf: AnnualWorkflow, *, user=None) -> AnnualWorkflow:
    """Put the ratification and audit-waiver votes on the agenda (draft votes; the board
    opens them from Votes). Each is created once."""
    from homeoa.votes import services as votes
    from homeoa.votes.models import Quorum, Threshold, VoteKind

    wf = AnnualWorkflow.all_associations.select_for_update().get(pk=wf.pk)
    meeting = wf.meeting
    if not meeting.is_open:
        raise AnnualError(_("A held or cancelled meeting cannot be changed."))
    a = wf.association
    opens, closes = _vote_times(meeting)
    if wf.ratification_vote_id is None:
        _require_adopted(wf)
        quorum_required = rules.resolve(a, QUORUM_REQUIRED_KEY, meeting.date).value
        title = _("Ratify the %(budget)s") % {"budget": wf.budget}
        wf.ratification_vote = votes.create_vote(
            a,
            title=title,
            kind=VoteKind.MEETING,
            opens_at=opens,
            closes_at=closes,
            questions=[
                (
                    _("Do you reject the %(budget)s?") % {"budget": wf.budget},
                    [
                        (_("Reject the budget"), {"affirmative": True}),
                        _("Do not reject"),
                        (_("Abstain"), {"abstain": True}),
                    ],
                )
            ],
            threshold=Threshold.RULE_PACK,
            pack_kind="budget_rejection",
            quorum=Quorum.RULE_PACK if quorum_required else Quorum.NONE,
            description=_(
                "The budget stands unless the rule pack's share of all votes rejects it."
            ),
            user=user,
        )
        services.add_agenda_item(meeting, title=title)
    if wf.waiver_vote_id is None:
        title = _("Waive the %(year)s audit") % {"year": meeting.date.year}
        wf.waiver_vote = votes.create_vote(
            a,
            title=title,
            kind=VoteKind.MEETING,
            opens_at=opens,
            closes_at=closes,
            questions=[
                (
                    _("Do you vote to waive the audit of the %(year)s financial statements?")
                    % {"year": meeting.date.year},
                    [
                        (_("Waive the audit"), {"affirmative": True}),
                        _("Do not waive"),
                        (_("Abstain"), {"abstain": True}),
                    ],
                )
            ],
            threshold=Threshold.RULE_PACK,
            pack_kind="audit_waiver",
            user=user,
        )
        services.add_agenda_item(meeting, title=title)
    wf.save(update_fields=["ratification_vote", "waiver_vote"])
    return wf


def _passed(vote) -> bool | None:
    from homeoa.votes.models import VoteStatus

    if vote is None or vote.status != VoteStatus.CLOSED or not vote.result:
        return None
    questions = vote.result.get("questions") or []
    return bool(questions and questions[0].get("passed"))


@transaction.atomic
def apply_results(wf: AnnualWorkflow, *, user=None) -> list[str]:
    """Record the closed votes' results: the budget ratified or rejected, and a passed
    audit waiver attached to the year's audit obligation period. Returns messages."""
    wf = AnnualWorkflow.all_associations.select_for_update().get(pk=wf.pk)
    out: list[str] = []
    rejected = _passed(wf.ratification_vote)
    if rejected is not None and not wf.ratification_outcome:
        outcome = AnnualWorkflow.Outcome.REJECTED if rejected else AnnualWorkflow.Outcome.RATIFIED
        wf.ratification_outcome = outcome
        budget = wf.budget
        budget.ratification = outcome
        budget.ratification_on = timezone.localdate(
            wf.ratification_vote.closed_at, timezone=_tz(wf.association)
        )
        budget.save(update_fields=["ratification", "ratification_on"])
        audit.record("budget.ratification", budget, changes={"outcome": outcome}, actor=user)
        out.append(
            _("Owners rejected the budget; the prior budget continues.")
            if rejected
            else _("The budget is ratified.")
        )
    waived = _passed(wf.waiver_vote)
    if waived is not None and not wf.waiver_outcome:
        wf.waiver_outcome = (
            AnnualWorkflow.Outcome.WAIVED if waived else AnnualWorkflow.Outcome.NOT_WAIVED
        )
        if waived:
            out.append(_attach_waiver(wf, user))
        else:
            out.append(_("The audit waiver did not pass: the annual audit is required."))
    wf.save(update_fields=["ratification_outcome", "waiver_outcome"])
    return out


def _attach_waiver(wf: AnnualWorkflow, user) -> str:
    from homeoa.compliance import services as compliance
    from homeoa.compliance.models import ObligationPeriod

    a, year = wf.association, wf.meeting.date.year

    def find():
        return ObligationPeriod.all_associations.filter(
            association=a, obligation_id=AUDIT_OBLIGATION, year=year
        ).first()

    period = find()
    if period is None:
        compliance.run(a, wf.meeting.date)
        period = find()
    if period is None:
        return _("The audit is waived. The rule pack has no audit obligation to close.")
    if not period.is_open:
        return _("The audit is waived. The %(year)s audit period was already closed.") % {
            "year": year
        }
    ob = compliance.obligation_for(period)
    path = next((p for p in (ob.paths if ob else ()) if p.threshold_key == WAIVER_KEY), None)
    if path is None:
        return _("The audit is waived. The audit obligation has no waiver path to close.")
    compliance.choose_path(period, path.id, user=user)
    compliance.add_evidence(
        period,
        url=reverse("votes:detail", args=[wf.waiver_vote_id]),
        note=_("Owner vote: %(vote)s") % {"vote": wf.waiver_vote},
        user=user,
    )
    compliance.complete(period, user=user, on=wf.meeting.date)
    return _("The audit is waived and the %(year)s audit obligation is closed.") % {"year": year}


# ---------------------------------------------------------------- to-dos


def dismiss_rejection(wf: AnnualWorkflow, *, user=None) -> AnnualWorkflow:
    """Dismiss the "budget rejected" follow-up to-do."""
    if wf.ratification_outcome != AnnualWorkflow.Outcome.REJECTED:
        raise AnnualError(_("The budget was not rejected."))
    if wf.rejection_dismissed_at is None:
        wf.rejection_dismissed_at = timezone.now()
        wf.save(update_fields=["rejection_dismissed_at"])
        audit.record("meetings.budget_rejection_dismissed", wf, actor=user)
    return wf


def rejection_todo_items(association) -> list[dict]:
    """A rejected budget stays adopted and the prior ratified budget continues;
    nothing changes automatically. The board confirms dues and starts a revised budget.
    Clears when a later budget for that year is ratified, or when dismissed."""
    from homeoa.access import permissions as perms
    from homeoa.budget.models import Budget

    items = []
    for wf in AnnualWorkflow.all_associations.filter(
        association=association,
        ratification_outcome=AnnualWorkflow.Outcome.REJECTED,
        rejection_dismissed_at__isnull=True,
        budget__isnull=False,
    ).select_related("meeting", "budget"):
        if (
            Budget.all_associations.filter(
                association=association,
                year=wf.budget.year,
                ratification=AnnualWorkflow.Outcome.RATIFIED,
            )
            .exclude(pk=wf.budget_id)
            .exists()
        ):
            continue
        items.append(
            {
                "id": f"meetings-budget-rejected-{wf.pk}",
                "kind": "gov",
                "title": _(
                    "Budget rejected: confirm the dues schedule still matches the prior "
                    "budget, and start a revised budget"
                ),
                "sub": _("%(year)s budget rejected by owners") % {"year": wf.budget.year},
                "due": wf.budget.ratification_on or association_today(association),
                "url": reverse("meetings:annual", args=[wf.meeting_id]),
                "action": _("Open"),
                "perm": perms.GOVERNANCE_MANAGE,
            }
        )
    return items


def todo_items(association) -> list[dict]:
    from homeoa.access import permissions as perms

    today = association_today(association)
    items = rejection_todo_items(association)
    for wf in AnnualWorkflow.all_associations.filter(
        association=association,
        meeting__status__in=(Meeting.Status.DRAFT, Meeting.Status.NOTICED),
        meeting__date__gte=today,
    ).select_related("meeting", "budget"):
        meeting = wf.meeting
        url = reverse("meetings:annual", args=[meeting.pk])
        by_key = {s.key: s for s in steps(wf, today)}
        send, notice = by_key["send"], by_key["notice"]
        pending = [s for s in (send, notice) if not s.done and s.due is not None]
        # The Treasurer adopts the budget; the Secretary sends the notice and summary.
        perm = perms.GOVERNANCE_MANAGE
        if not by_key["adopt"].done:
            title = _("Adopt next year's budget for the annual meeting %(date)s") % {
                "date": date_format(meeting.date, "M j")
            }
            due = send.due or meeting.date
            perm = perms.FINANCE_MANAGE
        elif len(pending) == 2:
            due = min(send.due, notice.due)
            title = _("Send annual meeting notice & budget summary by %(date)s") % {
                "date": date_format(due, "M j")
            }
        elif pending:
            due = pending[0].due
            title = (
                _("Send the budget summary by %(date)s")
                if pending[0] is send
                else _("Send annual meeting notice by %(date)s")
            ) % {"date": date_format(due, "M j")}
        else:
            continue
        items.append(
            {
                "id": f"meetings-annual-{meeting.pk}",
                "kind": "gov",
                "title": title,
                "sub": _("Annual meeting %(date)s") % {"date": date_format(meeting.date, "M j, Y")},
                "due": due,
                "url": url,
                "action": _("Open"),
                "perm": perm,
            }
        )
    return items
