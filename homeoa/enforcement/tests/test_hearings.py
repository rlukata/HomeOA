"""Hearings, decisions and fines on the owner ledger (VIO-04, VIO-05)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse
from django.utils.formats import date_format

from homeoa import rules
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import Charge, ChargeKind
from homeoa.billing.owner_ledger import owner_ledger
from homeoa.enforcement import hearings, ladder
from homeoa.enforcement.models import (
    FineSchedule,
    FineScheduleLine,
    Hearing,
    Violation,
    ViolationCategory,
    ViolationStep,
)
from homeoa.enforcement.services import EnforcementError, log_violation
from homeoa.meetings.tests.test_meetings import TODAY, User, _consent, pilot  # noqa: F401
from homeoa.notify.models import Notification
from homeoa.registry.models import Unit

D = dt.date
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
OBSERVED = D(2026, 9, 1)
LATER = D(2026, 11, 20)  # after the hearing and the cure deadline
FURNISHED = D(2026, 8, 1)


def _schedule(a, furnished_on=FURNISHED, first="50.00", repeat="100.00"):
    s = FineSchedule.objects.create(
        association=a,
        version=1,
        effective_on=D(2026, 1, 1),
        adopted_on=D(2026, 1, 1),
        furnished_on=furnished_on,
    )
    cat = _category(a)
    FineScheduleLine.objects.create(
        association=a,
        schedule=s,
        category=cat,
        first_amount=Decimal(first),
        repeat_amount=Decimal(repeat),
    )
    return s


def _category(a):
    return ViolationCategory.objects.get_or_create(
        association=a, name="Pets", defaults={"rule_citation": "Rules §4.2"}
    )[0]


def _violation(a, number="A", observed=OBSERVED):
    return log_violation(
        a,
        unit=Unit.objects.get(number=number),
        category=_category(a),
        observed_on=observed,
        description="Dog off leash in the lobby",
        photos=[("dog.png", PNG)],
    )


def _min_days(a):
    return hearings.notice_min_days(a, TODAY).value


def _heard(a, number="A", observed=OBSERVED):
    """A violation with both notices sent today and a hearing at the earliest date."""
    v = _violation(a, number, observed)
    ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    ladder.send_notice(v, ViolationStep.Kind.FORMAL)
    h = hearings.schedule_hearing(
        v, hearing_on=TODAY + dt.timedelta(days=_min_days(a)), place="Community room"
    )
    return v, h


def _fine(h, today=LATER, **kw):
    return hearings.record_decision(h, outcome=Hearing.Outcome.FINE, today=today, **kw)


# --- scheduling -------------------------------------------------------------------------


def test_hearing_notice_respects_min_days_from_pack(pilot):  # noqa: F811
    v = _violation(pilot)
    ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    with pytest.raises(EnforcementError, match="formal"):
        hearings.schedule_hearing(v, hearing_on=D(2026, 12, 1), place="Lobby")
    ladder.send_notice(v, ViolationStep.Kind.FORMAL)
    days = _min_days(pilot)
    with pytest.raises(EnforcementError, match="at least"):
        hearings.schedule_hearing(v, hearing_on=TODAY + dt.timedelta(days=days - 1), place="Lobby")
    h = hearings.schedule_hearing(v, hearing_on=TODAY + dt.timedelta(days=days), place="Lobby")
    assert h.notice_step.kind == ViolationStep.Kind.HEARING_NOTICE
    assert h.notice_step.letter.subject.startswith("Notice of hearing")
    with pytest.raises(EnforcementError, match="already"):
        hearings.schedule_hearing(v, hearing_on=D(2026, 12, 31), place="Lobby")
    assert AuditEvent.objects.filter(action="enforcement.hearing_scheduled").exists()


def test_min_days_follows_override(pilot):  # noqa: F811
    rules.set_override(pilot, "enforcement.hearing_notice_min_days", 30, reason="Bylaws 7.2")
    v = _violation(pilot)
    ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    ladder.send_notice(v, ViolationStep.Kind.FORMAL)
    with pytest.raises(EnforcementError):
        hearings.schedule_hearing(v, hearing_on=TODAY + dt.timedelta(days=29), place="x")
    hearings.schedule_hearing(v, hearing_on=TODAY + dt.timedelta(days=30), place="x")


# --- fine gates -------------------------------------------------------------------------


def test_fine_blocked_before_hearing(pilot):  # noqa: F811
    _schedule(pilot)
    v, h = _heard(pilot)
    with pytest.raises(EnforcementError, match="after the hearing"):
        _fine(h, today=h.hearing_on - dt.timedelta(days=1))
    assert not Charge.objects.filter(kind=ChargeKind.FINE).exists()
    assert "before the hearing" in hearings.fine_problem(h, h.hearing_on - dt.timedelta(days=1))


def test_fine_blocked_before_cure_deadline(pilot):  # noqa: F811
    _schedule(pilot)
    rules.set_override(pilot, "enforcement.cure_days", 60, reason="Bylaws")
    v, h = _heard(pilot)
    cure_by = ladder.formal_step(v).cure_by
    assert h.hearing_on < cure_by
    with pytest.raises(EnforcementError, match="cure deadline"):
        _fine(h, today=cure_by)
    _fine(h, today=cure_by + dt.timedelta(days=1))


@pytest.mark.parametrize("furnished_on", [None, D(2026, 9, 2)])
def test_fine_blocked_without_furnished_schedule(pilot, furnished_on):  # noqa: F811
    _schedule(pilot, furnished_on=furnished_on)
    v, h = _heard(pilot)
    with pytest.raises(EnforcementError, match="not furnished"):
        _fine(h)
    # No override, not even a different amount with a reason.
    with pytest.raises(EnforcementError, match="not furnished"):
        _fine(h, fine_amount=Decimal("10.00"), amount_reason="board says so")
    assert not Charge.objects.filter(kind=ChargeKind.FINE).exists()
    h.refresh_from_db()
    assert h.outcome == ""


def test_fine_blocked_without_schedule(pilot):  # noqa: F811
    v, h = _heard(pilot)
    with pytest.raises(EnforcementError, match="No fine schedule"):
        _fine(h)


def test_furnished_on_violation_date_is_enough(pilot):  # noqa: F811
    _schedule(pilot, furnished_on=OBSERVED)
    v, h = _heard(pilot)
    assert _fine(h).charge is not None


# --- posting ----------------------------------------------------------------------------


def test_fine_posts_balanced_in_operating_and_shows_on_ledger(pilot):  # noqa: F811
    _schedule(pilot)
    v, h = _heard(pilot)
    h = _fine(h, attended=True, owner_statement="Dog was mine; sorry.")
    c = h.charge
    assert c.kind == ChargeKind.FINE and c.amount == Decimal("50.00")
    assert h.schedule_amount == Decimal("50.00") and h.amount_reason == ""
    lines = list(c.entry.lines.select_related("account", "fund"))
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines) == Decimal("50.00")
    assert {ln.fund.code for ln in lines} == {"operating"}
    assert {ln.account.code for ln in lines} == {"1100", "4150"}
    rows = owner_ledger(v.unit)
    assert any("Violation fine" in r.description and r.charge == Decimal("50.00") for r in rows)
    v.refresh_from_db()
    assert v.status == Violation.Status.CLOSED
    assert h.decision_step.kind == ViolationStep.Kind.DECISION
    assert h.decision_step.letter.subject.startswith("Hearing decision")
    ev = AuditEvent.objects.filter(action="enforcement.hearing_decided").latest("pk")
    assert ev.changes["fine_amount"] == "50.00" and ev.changes["charge"] == c.pk


def test_fine_on_owner_statement(pilot):  # noqa: F811
    from homeoa.statements.owner_statements import compute

    _schedule(pilot)
    v, h = _heard(pilot)
    c = _fine(h).charge
    out = compute(c.ownership_period, D(2026, 11, 1), D(2026, 11, 30))
    assert any("Violation fine" in r["description"] for r in out["rows"])
    assert out["charges"] >= Decimal("50.00")


def test_amount_editable_with_reason_and_repeat_default(pilot):  # noqa: F811
    _schedule(pilot)
    v, h = _heard(pilot)
    with pytest.raises(EnforcementError, match="reason"):
        _fine(h, fine_amount=Decimal("75.00"))
    h = _fine(h, fine_amount=Decimal("75.00"), amount_reason="Second dog")
    assert h.fine_amount == Decimal("75.00") and h.schedule_amount == Decimal("50.00")
    # Same unit, same category again: the repeat amount applies.
    v2 = _violation(pilot, observed=D(2026, 9, 20))
    assert hearings.is_repeat(v2)
    assert hearings.schedule_amount(v2) == Decimal("100.00")
    # Another unit is a first offense.
    assert hearings.schedule_amount(_violation(pilot, "B")) == Decimal("50.00")


def test_void_reverses_and_is_audited(pilot):  # noqa: F811
    _schedule(pilot)
    v, h = _heard(pilot)
    h = _fine(h)
    with pytest.raises(EnforcementError, match="reason"):
        hearings.void_fine(h, "", today=LATER)
    hearings.void_fine(h, "Decided in error", today=LATER)
    c = Charge.objects.get(pk=h.charge_id)
    assert c.void_entry_id and c.open_balance == 0
    assert sum(r.charge - r.payment for r in owner_ledger(v.unit) if "fine" in r.description) == 0
    assert AuditEvent.objects.filter(action="enforcement.fine_voided").exists()
    assert AuditEvent.objects.filter(action="billing.charge_void").exists()
    with pytest.raises(EnforcementError):
        hearings.void_fine(h, "again", today=LATER)
    # A voided fine is not a prior offense.
    assert not hearings.is_repeat(_violation(pilot, observed=D(2026, 9, 20)))


def test_allocation_puts_fines_with_other_charges(pilot):  # noqa: F811
    from homeoa.payments import services as pay

    assert pay.bucket(ChargeKind.FINE) == "other"
    _schedule(pilot)
    v, h = _heard(pilot)
    fine = _fine(h).charge
    assessment = Charge.objects.filter(unit=v.unit, kind=ChargeKind.ASSESSMENT).first()
    if assessment is None:
        pytest.skip("demo seed has no assessment charge")
    ordered = pay.order_charges([fine, assessment], ("assessment", "late_fee", "interest", "other"))
    assert ordered == [assessment, fine]


def test_late_fee_run_ignores_fines(pilot):  # noqa: F811
    from django.core.exceptions import ValidationError

    from homeoa.billing.latefee_models import LateFeePolicy
    from homeoa.billing.services import latefees

    _schedule(pilot)
    v, h = _heard(pilot)
    fine = _fine(h).charge
    LateFeePolicy.objects.all().delete()
    kw = latefees.default_policy_kwargs(pilot)
    kw.update(applies_to=["assessment", "fine"], interest_apr=Decimal("12"))
    policy = LateFeePolicy(association=pilot, effective_start=D(2026, 1, 1), **kw)
    with pytest.raises(ValidationError):
        policy.full_clean()
    policy.save()  # even a policy saved around validation never touches fines
    latefees.assess(policy, D(2027, 3, 1))
    assert not latefees.LateFee.objects.filter(source_charge=fine).exists()


# --- other outcomes ---------------------------------------------------------------------


def test_dismissed_and_cured_outcomes_post_nothing(pilot):  # noqa: F811
    v, h = _heard(pilot)
    hearings.record_decision(h, outcome=Hearing.Outcome.DISMISSED, today=LATER)
    v.refresh_from_db()
    assert v.status == Violation.Status.DISMISSED
    v2, h2 = _heard(pilot, "B")
    hearings.record_decision(h2, outcome=Hearing.Outcome.CURED, attended=False, today=LATER)
    v2.refresh_from_db()
    assert v2.status == Violation.Status.CURED and v2.cured_on == LATER
    assert not Charge.objects.filter(kind=ChargeKind.FINE).exists()
    with pytest.raises(EnforcementError, match="already"):
        hearings.record_decision(h2, outcome=Hearing.Outcome.FINE, today=LATER)


# --- delivery ---------------------------------------------------------------------------


@pytest.mark.deferred_notify
def test_no_decision_email_on_rollback(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    _schedule(pilot)
    v, h = _heard(pilot, "C")
    before = Notification.objects.filter(unit=v.unit).count()
    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        _fine(h)
        raise RuntimeError("boom")
    assert callbacks == []
    assert Notification.objects.filter(unit=v.unit).count() == before
    assert not Charge.objects.filter(kind=ChargeKind.FINE).exists()
    h.refresh_from_db()
    assert h.outcome == ""


@pytest.mark.deferred_notify
def test_decision_email_after_commit(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    _schedule(pilot)
    v, h = _heard(pilot, "C")
    with django_capture_on_commit_callbacks(execute=True):
        h = _fine(h)
    assert h.decision_step.delivery == ViolationStep.Delivery.EMAIL
    assert Notification.objects.filter(type_key=ladder.NOTIFICATION_TYPE, unit=v.unit).exists()


# --- to-do and views --------------------------------------------------------------------


def test_todos(pilot, settings):  # noqa: F811
    from homeoa.enforcement.todos import todo_items

    v, h = _heard(pilot)
    settings.HOMEOA_FIXED_TODAY = LATER.isoformat()
    titles = [i["title"] for i in todo_items(pilot)]
    assert "Record hearing decision" in titles
    assert "Cure deadline passed: schedule hearing" not in titles


def test_views_schedule_decide_void(pilot, client):  # noqa: F811
    _schedule(pilot)
    board = User.objects.create_user(email="hb@example.com", password="x-pass-12345")
    grant(board, association=pilot)
    client.force_login(board)
    v = _violation(pilot)
    ladder.send_notice(v, ViolationStep.Kind.COURTESY)
    ladder.send_notice(v, ViolationStep.Kind.FORMAL)
    url = reverse("enforcement:detail", args=[v.pk])
    assert b"hearing-form" in client.get(url).content
    on = TODAY + dt.timedelta(days=_min_days(pilot))
    client.post(
        reverse("enforcement:hearing", args=[v.pk]),
        {"hearing_on": on.isoformat(), "hearing_time": "19:00", "place": "Lobby"},
    )
    h = Hearing.objects.get(violation=v)
    assert h.hearing_time == dt.time(19, 0)
    assert b"decision-wait" in client.get(url).content
    h.hearing_on = D(2026, 10, 1)  # pretend the hearing was held; cure deadline not passed
    h.save()
    page = client.get(url).content
    assert b"fine-blocked" in page
    # Board without finance.manage cannot fine.
    r = client.post(reverse("enforcement:decision", args=[h.pk]), {"outcome": "fine"})
    assert r.status_code == 302
    h.refresh_from_db()
    assert h.outcome == ""
    client.post(
        reverse("enforcement:decision", args=[h.pk]),
        {"outcome": "dismissed", "attended": "yes", "owner_statement": "Not my dog"},
    )
    h.refresh_from_db()
    assert h.outcome == "dismissed" and h.attended is True


def test_views_fine_needs_finance_manage(pilot, client, settings):  # noqa: F811
    _schedule(pilot)
    treasurer = User.objects.create_user(email="ht@example.com", password="x-pass-12345")
    grant(treasurer, association=pilot, title="treasurer")
    client.force_login(treasurer)
    v, h = _heard(pilot)
    settings.HOMEOA_FIXED_TODAY = LATER.isoformat()
    r = client.post(
        reverse("enforcement:decision", args=[h.pk]),
        {"outcome": "fine", "fine_amount": "50.00"},
    )
    assert r.status_code == 302
    h.refresh_from_db()
    assert h.outcome == "fine" and h.charge.amount == Decimal("50.00")
    client.post(reverse("enforcement:fine_void", args=[h.pk]), {"reason": "Error"})
    assert Charge.objects.get(pk=h.charge_id).void_entry_id


def test_views_need_permission(pilot, client):  # noqa: F811
    owner = User.objects.create_user(email="ho@example.com", password="x-pass-12345")
    client.force_login(owner)
    v = _violation(pilot)
    r = client.post(
        reverse("enforcement:hearing", args=[v.pk]), {"hearing_on": "2026-12-01", "place": "x"}
    )
    assert r.status_code in (302, 403) and not Hearing.objects.exists()


# --- reschedule and cancel --------------------------------------------------------


def test_reschedule_keeps_min_days_and_sends_updated_notice(pilot):  # noqa: F811
    v, h = _heard(pilot)
    days = _min_days(pilot)
    first_step = h.notice_step_id
    with pytest.raises(EnforcementError, match="at least"):
        hearings.reschedule_hearing(h, hearing_on=TODAY + dt.timedelta(days=days - 1), place="x")
    new_date = TODAY + dt.timedelta(days=days + 7)
    h = hearings.reschedule_hearing(h, hearing_on=new_date, place="Lobby", online=True)
    h.refresh_from_db()
    assert h.hearing_on == new_date and h.place == "Lobby" and h.online
    assert h.notice_step_id != first_step
    assert h.notice_step.kind == ViolationStep.Kind.HEARING_NOTICE
    assert h.notice_step.letter.subject.startswith("Notice of hearing")
    assert v.steps.filter(kind=ViolationStep.Kind.HEARING_NOTICE).count() == 2
    ev = AuditEvent.objects.get(action="enforcement.hearing_rescheduled")
    assert ev.changes["hearing_on"] == new_date.isoformat()
    assert ev.changes["before"]["place"] == "Community room"


def test_reschedule_follows_override(pilot):  # noqa: F811
    v, h = _heard(pilot)
    rules.set_override(pilot, "enforcement.hearing_notice_min_days", 30, reason="Bylaws 7.2")
    with pytest.raises(EnforcementError, match="at least 30"):
        hearings.reschedule_hearing(h, hearing_on=TODAY + dt.timedelta(days=29), place="x")
    hearings.reschedule_hearing(h, hearing_on=TODAY + dt.timedelta(days=30), place="x")


def test_no_updated_notice_email_on_rollback(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v, h = _heard(pilot, "C")
    before = Notification.objects.filter(unit=v.unit).count()
    new_date = TODAY + dt.timedelta(days=_min_days(pilot) + 1)
    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        hearings.reschedule_hearing(h, hearing_on=new_date, place="Lobby")
        raise RuntimeError("boom")
    assert callbacks == []
    assert Notification.objects.filter(unit=v.unit).count() == before
    h.refresh_from_db()
    assert h.place == "Community room"


@pytest.mark.deferred_notify
def test_updated_notice_email_after_commit(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v, h = _heard(pilot, "C")
    before = Notification.objects.filter(type_key=ladder.NOTIFICATION_TYPE, unit=v.unit).count()
    new_date = TODAY + dt.timedelta(days=_min_days(pilot) + 1)
    with django_capture_on_commit_callbacks(execute=True):
        h = hearings.reschedule_hearing(h, hearing_on=new_date, place="Lobby")
    assert h.notice_step.delivery == ViolationStep.Delivery.EMAIL
    after = Notification.objects.filter(type_key=ladder.NOTIFICATION_TYPE, unit=v.unit).count()
    assert after == before + 1


def test_cancel_reenables_scheduling(pilot):  # noqa: F811
    _schedule(pilot)
    v, h = _heard(pilot)
    assert hearings.schedule_problem(v, None, TODAY) != ""
    hearings.cancel_hearing(h, "Owner asked to postpone")
    h.refresh_from_db()
    assert h.cancelled and h.cancel_reason == "Owner asked to postpone"
    assert hearings.pending_hearing(v) is None
    assert hearings.schedule_problem(v, None, TODAY) == ""
    ev = AuditEvent.objects.get(action="enforcement.hearing_cancelled")
    assert ev.changes["hearing"] == h.pk
    with pytest.raises(EnforcementError, match="cancelled"):
        hearings.cancel_hearing(h)
    with pytest.raises(EnforcementError, match="cancelled"):
        hearings.reschedule_hearing(h, hearing_on=D(2026, 12, 31), place="x")
    with pytest.raises(EnforcementError, match="cancelled"):
        _fine(h)
    h2 = hearings.schedule_hearing(
        v, hearing_on=TODAY + dt.timedelta(days=_min_days(pilot)), place="Lobby"
    )
    assert hearings.pending_hearing(v) == h2


def test_decided_hearing_cannot_be_changed(pilot):  # noqa: F811
    v, h = _heard(pilot)
    h = hearings.record_decision(h, outcome=Hearing.Outcome.DISMISSED, today=LATER)
    with pytest.raises(EnforcementError, match="decision"):
        hearings.cancel_hearing(h)
    with pytest.raises(EnforcementError, match="decision"):
        hearings.reschedule_hearing(h, hearing_on=D(2026, 12, 31), place="x")


def test_cancelled_hearing_has_no_decision_todo(pilot, settings):  # noqa: F811
    from homeoa.enforcement.todos import todo_items

    v, h = _heard(pilot)
    hearings.cancel_hearing(h)
    settings.HOMEOA_FIXED_TODAY = LATER.isoformat()
    titles = [i["title"] for i in todo_items(pilot)]
    assert "Record hearing decision" not in titles
    assert "Cure deadline passed: schedule hearing" in titles


def test_views_reschedule_and_cancel(pilot, client):  # noqa: F811
    board = User.objects.create_user("hearing-board@example.org")
    grant(board, association=pilot)
    client.force_login(board)
    v, h = _heard(pilot)
    url = reverse("enforcement:detail", args=[v.pk])
    page = client.get(url).content
    assert b"reschedule-form" in page and b"cancel-form" in page
    new_date = TODAY + dt.timedelta(days=_min_days(pilot) + 3)
    r = client.post(
        reverse("enforcement:reschedule", args=[h.pk]),
        {"hearing_on": new_date.isoformat(), "hearing_time": "18:30", "place": "Clubhouse"},
    )
    assert r.status_code == 302
    h.refresh_from_db()
    assert h.hearing_on == new_date and h.hearing_time == dt.time(18, 30)
    r = client.post(reverse("enforcement:hearing_cancel", args=[h.pk]), {"reason": "Moved"})
    assert r.status_code == 302
    h.refresh_from_db()
    assert h.cancelled
    page = client.get(url).content
    assert b"hearing-cancelled" in page and b"hearing-form" in page


# --- hearing-cancelled letter ---------------------------------------------------


@pytest.mark.deferred_notify
def test_cancel_sends_letter_after_commit(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v, h = _heard(pilot, "C")
    original = h.hearing_on
    with django_capture_on_commit_callbacks(execute=True):
        hearings.cancel_hearing(h, "Board member ill")
    step = v.steps.get(kind=ViolationStep.Kind.HEARING_CANCELLED)
    assert step.delivery == ViolationStep.Delivery.EMAIL
    assert step.letter.subject.startswith("Hearing cancelled")
    n = Notification.objects.filter(type_key=ladder.NOTIFICATION_TYPE, unit=v.unit).latest("pk")
    assert n.subject.startswith("Hearing cancelled")
    assert date_format(original, "F j, Y") in n.body
    assert "Board member ill" in n.body
    ev = AuditEvent.objects.get(action="enforcement.hearing_cancelled")
    assert ev.changes["letter_step"] == step.pk


def test_cancel_letter_delivered_like_hearing_notice(pilot):  # noqa: F811
    v, h = _heard(pilot)
    hearings.cancel_hearing(h)
    step = v.steps.get(kind=ViolationStep.Kind.HEARING_CANCELLED)
    assert step.delivery == h.notice_step.delivery
    assert (step.batch is None) == (step.delivery == ViolationStep.Delivery.EMAIL)


@pytest.mark.deferred_notify
def test_no_cancel_letter_on_rollback(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v, h = _heard(pilot, "C")
    before = Notification.objects.filter(unit=v.unit).count()
    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        hearings.cancel_hearing(h, "x")
        raise RuntimeError("boom")
    assert callbacks == []
    assert Notification.objects.filter(unit=v.unit).count() == before
    assert not v.steps.filter(kind=ViolationStep.Kind.HEARING_CANCELLED).exists()
    h.refresh_from_db()
    assert not h.cancelled


@pytest.mark.deferred_notify
def test_reschedule_email_shows_new_date(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v, h = _heard(pilot, "C")
    new_date = TODAY + dt.timedelta(days=_min_days(pilot) + 5)
    with django_capture_on_commit_callbacks(execute=True):
        hearings.reschedule_hearing(h, hearing_on=new_date, place="Lobby")
    n = Notification.objects.filter(type_key=ladder.NOTIFICATION_TYPE, unit=v.unit).latest("pk")
    assert n.subject.startswith("Notice of hearing")
    assert date_format(new_date, "F j, Y") in n.body and "Lobby" in n.body
