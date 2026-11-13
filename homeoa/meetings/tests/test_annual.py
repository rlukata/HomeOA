"""Annual meeting workflow, budget ratification and audit-waiver vote."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from homeoa.budget import services as budget_services
from homeoa.budget.models import Budget
from homeoa.compliance.models import ObligationPeriod, PeriodStatus
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos
from homeoa.meetings import annual
from homeoa.meetings.models import AnnualWorkflow, Meeting
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import Unit
from homeoa.votes import services as votes
from homeoa.votes.models import Vote, VoteStatus

D = dt.date
TODAY = D(2026, 10, 7)
DEC3 = D(2026, 12, 3)
User = get_user_model()


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def _wf(a):
    return AnnualWorkflow.objects.get(meeting__date=DEC3)


OCT5 = D(2026, 10, 5)


def _adopt(wf, on=OCT5):
    budget = budget_services.adopt(wf.budget)
    Budget.objects.filter(pk=budget.pk).update(
        adopted_at=dt.datetime.combine(on, dt.time(12), tzinfo=dt.UTC)
    )
    wf.refresh_from_db()
    return wf


def _vote(vote, yes_units, no_units=()):
    question = vote.questions.get()
    yes = question.choices.get(affirmative=True)
    no = question.choices.get(affirmative=False, abstain=False)
    votes.open_vote(vote)
    at = vote.closes_at - dt.timedelta(hours=1)
    for number in yes_units:
        unit = Unit.objects.get(number=number)
        votes.cast_ballot(vote, unit, reg.current_owners(unit, DEC3)[0], {question: yes}, now=at)
    for number in no_units:
        unit = Unit.objects.get(number=number)
        votes.cast_ballot(vote, unit, reg.current_owners(unit, DEC3)[0], {question: no}, now=at)
    return votes.close_vote(vote, now=at)


def test_demo_has_dec3_meeting_with_2027_draft(pilot):
    wf = _wf(pilot)
    assert wf.meeting.kind == Meeting.Kind.ANNUAL
    assert wf.budget.year == 2027 and wf.budget.is_draft


def test_step_dates_from_pack_for_dec3(pilot):
    wf = _adopt(_wf(pilot))
    steps = {s.key: s for s in annual.steps(wf, TODAY)}
    assert steps["adopt"].done and steps["adopt"].done_on == D(2026, 10, 5)
    # 30 days after adoption (Nov 4) is before Dec 3 - 14 (Nov 19).
    assert steps["send"].due == D(2026, 11, 4)
    assert steps["notice"].due == D(2026, 11, 19)
    annual.mark_budget_sent(wf, D(2026, 11, 1))
    steps = {s.key: s for s in annual.steps(wf, TODAY)}
    assert steps["meeting"].window == (D(2026, 11, 15), D(2026, 12, 21))
    assert not steps["meeting"].problem
    annual.mark_budget_sent(wf, D(2026, 11, 25))
    steps = {s.key: s for s in annual.steps(wf, TODAY)}
    assert "too soon" in steps["meeting"].problem


def test_todo_combines_notice_and_summary(pilot):
    wf = _adopt(_wf(pilot))
    items = [t for t in todos.collect(pilot) if t.source == "annual_meeting"]
    assert len(items) == 1
    assert items[0].title == "Send annual meeting notice & budget summary by Nov 4"
    assert not [t for t in todos.collect(pilot) if t.id == f"meetings-notice-{wf.meeting_id}"]


def test_adopt_todo_goes_to_the_treasurer_then_the_secretary(pilot):
    from homeoa.access import permissions as perms

    wf = _wf(pilot)
    (item,) = [t for t in todos.collect(pilot) if t.source == "annual_meeting"]
    assert item.title.startswith("Adopt next year's budget")
    assert item.perm == perms.FINANCE_MANAGE
    _adopt(wf)
    (item,) = [t for t in todos.collect(pilot) if t.source == "annual_meeting"]
    assert item.perm == perms.GOVERNANCE_MANAGE


def test_missing_adopted_budget_blocks_summary(pilot):
    wf = _wf(pilot)
    steps = {s.key: s for s in annual.steps(wf, TODAY)}
    assert steps["send"].problem
    with pytest.raises(annual.AnnualError, match="not adopted"):
        annual.mark_budget_sent(wf, TODAY)
    with pytest.raises(annual.AnnualError, match="not adopted"):
        annual.create_votes(wf)


def test_votes_on_agenda_with_pack_thresholds(pilot):
    wf = _adopt(_wf(pilot))
    annual.mark_budget_sent(wf, D(2026, 11, 1))
    wf = annual.create_votes(wf)
    annual.create_votes(wf)  # idempotent
    assert Vote.objects.count() == 2
    assert wf.meeting.agenda.count() == 2
    rule = votes.threshold_rule(wf.waiver_vote)
    assert str(rule.percent) == "60"
    assert votes.threshold_rule(wf.ratification_vote).comparison == "more_than"


def test_rejected_by_majority_of_all_votes(pilot):
    wf = annual.create_votes(_adopt(_wf(pilot)))
    _vote(wf.ratification_vote, ["A", "B", "C", "D", "E"])
    annual.apply_results(wf)
    wf.refresh_from_db()
    assert wf.ratification_outcome == "rejected"
    assert wf.budget.ratification == "rejected"


def test_not_rejected_without_majority_of_all(pilot):
    # 4 of 9 reject, 1 against rejection: a majority of votes cast but not of all votes.
    wf = annual.create_votes(_adopt(_wf(pilot)))
    _vote(wf.ratification_vote, ["A", "B", "C", "D"], ["E"])
    annual.apply_results(wf)
    wf.refresh_from_db()
    assert wf.ratification_outcome == "ratified"
    assert wf.budget.ratification == "ratified"
    assert wf.budget.ratification_on is not None


def test_waiver_result_closes_audit_period(pilot):
    wf = annual.create_votes(_adopt(_wf(pilot)))
    _vote(wf.waiver_vote, ["A", "B", "C", "D", "E", "F"])
    msgs = annual.apply_results(wf)
    wf.refresh_from_db()
    assert wf.waiver_outcome == "waived"
    period = ObligationPeriod.objects.get(obligation_id="annual_audit", year=2026)
    assert period.status == PeriodStatus.DONE and period.path_id == "waiver"
    assert f"/votes/{wf.waiver_vote_id}/" in period.evidence.get().url
    assert "closed" in msgs[0]


def test_waiver_failing_leaves_period_open(pilot):
    wf = annual.create_votes(_adopt(_wf(pilot)))
    _vote(wf.waiver_vote, ["A", "B", "C", "D", "E"])
    annual.apply_results(wf)
    wf.refresh_from_db()
    assert wf.waiver_outcome == "not_waived"
    assert not ObligationPeriod.objects.filter(
        obligation_id="annual_audit", year=2026, status=PeriodStatus.DONE
    ).exists()


def test_open_votes_ignored_by_apply(pilot):
    wf = annual.create_votes(_adopt(_wf(pilot)))
    assert wf.waiver_vote.status == VoteStatus.DRAFT
    assert annual.apply_results(wf) == []


def test_annual_page_and_actions(pilot, client):
    user = User.objects.get(email="secretary@cedarrow.example")
    client.force_login(user)
    wf = _wf(pilot)
    url = f"/governance/meetings/{wf.meeting_id}/annual/"
    page = client.get(url)
    assert page.status_code == 200
    assert b"step-send" in page.content
    r = client.post(url + "do/", {"action": "sent", "on": "2026-11-01"}, follow=True)
    assert b"not adopted" in r.content
    _adopt(wf)
    client.post(url + "do/", {"action": "sent", "on": "2026-11-01"})
    client.post(url + "do/", {"action": "votes"})
    wf.refresh_from_db()
    assert wf.budget_sent_on == D(2026, 11, 1) and wf.waiver_vote_id
    assert timezone.localdate(wf.waiver_vote.closes_at) >= DEC3 - dt.timedelta(days=1)


def _rejected(pilot):
    wf = annual.create_votes(_adopt(_wf(pilot)))
    _vote(wf.ratification_vote, ["A", "B", "C", "D", "E"])
    annual.apply_results(wf)
    wf.refresh_from_db()
    return wf


def _rejection_todos(pilot, wf):
    return [t for t in todos.collect(pilot) if t.id == f"meetings-budget-rejected-{wf.pk}"]


def test_rejected_budget_todo_until_dismissed(pilot):
    wf = _rejected(pilot)
    assert wf.budget.status == "adopted"  # Nothing changes automatically
    items = _rejection_todos(pilot, wf)
    assert len(items) == 1 and items[0].title.startswith("Budget rejected: confirm the dues")
    annual.dismiss_rejection(wf)
    assert not _rejection_todos(pilot, wf)


def test_rejected_budget_todo_clears_when_later_budget_ratified(pilot):
    wf = _rejected(pilot)
    assert _rejection_todos(pilot, wf)
    later = budget_services.create_budget(
        pilot, wf.budget.year, name="Revised", copy_from=wf.budget
    )
    budget_services.adopt(later)
    assert _rejection_todos(pilot, wf)  # adopted but not yet ratified
    Budget.objects.filter(pk=later.pk).update(ratification="ratified")
    assert not _rejection_todos(pilot, wf)


def test_set_budget_and_apply_results_are_audited(pilot):
    from homeoa.audit.models import AuditEvent

    def n(wf):
        return AuditEvent.objects.filter(
            object_type="meetings.annualworkflow", object_id=str(wf.pk)
        ).count()

    wf = _wf(pilot)
    budget = wf.budget
    AnnualWorkflow.objects.filter(pk=wf.pk).update(budget=None)
    wf.refresh_from_db()
    before = n(wf)
    wf = annual.set_budget(wf, budget)
    assert n(wf) > before
    wf = _adopt(wf)
    wf = annual.create_votes(wf)
    _vote(wf.ratification_vote, ["A", "B", "C", "D", "E"])
    mid = n(wf)
    annual.apply_results(wf)
    assert n(wf) > mid
