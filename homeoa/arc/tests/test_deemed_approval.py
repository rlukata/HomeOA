# ruff: noqa: F811 (pytest fixtures imported from test_arc)
"""EV charger and heat pump requests are deemed approved by law when not denied in writing
in time (WA: 60 days, RCW 64.90.513(3)(c) and 64.90.580(3)(c))."""

import datetime as dt

import pytest

from homeoa import rules
from homeoa.arc import deemed, services
from homeoa.arc.models import ArcRequest, StatutoryKind
from homeoa.letters.models import Letter
from homeoa.notify.models import Notification

from .test_arc import GOOD, assoc, board, owner, unit  # noqa: F401
from .test_deadlines import at

D = dt.date
OCT1 = D(2026, 10, 1)


@pytest.fixture
def ev_form(assoc):
    rules.set_override(assoc, "arc.ev_charger.deemed_approval_days", 60, reason="test")
    form = services.ensure_default_form(assoc)
    return services.save_form(
        assoc,
        name="EV charger",
        fields=form.fields,
        statutory_kind=StatutoryKind.EV_CHARGER,
    )


def submit(assoc, person, form, day=OCT1):
    with at(day):
        return services.submit(
            assoc, unit=unit("A"), person=person, form=form, title="EV charger", data=GOOD
        )


def test_ev_charger_request_must_be_denied_within_60_days(assoc, ev_form):
    _, p = owner(assoc)
    req = submit(assoc, p, ev_form)
    assert req.statutory_kind == StatutoryKind.EV_CHARGER
    assert req.deny_by == D(2026, 11, 30)
    # The board's own 30-day deadline comes first.
    assert req.decide_by == D(2026, 10, 31)


def test_waiting_for_the_owners_answer_pauses_the_60_days(assoc, ev_form):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = submit(assoc, p, ev_form)
    with at(D(2026, 10, 10)):
        services.request_info(req, message="Which wall?", user=b_usr)
    with at(D(2026, 10, 20)):
        services.provide_info(req, message="North wall", user=None)
    req.refresh_from_db()
    assert req.paused_days == 10
    assert req.deny_by == D(2026, 12, 10)
    # A restarted 30-day board clock (Nov 19) still falls before the legal deadline.
    assert req.decide_by == D(2026, 11, 19)


def test_request_not_denied_in_time_is_recorded_as_approved(
    assoc, ev_form, django_capture_on_commit_callbacks
):
    _, p = owner(assoc)
    board(assoc, "chair@example.org", "president")
    req = submit(assoc, p, ev_form)
    assert deemed.record_deemed_approvals(assoc, D(2026, 11, 30)) == 0  # last day to deny
    with django_capture_on_commit_callbacks(execute=True):
        assert deemed.record_deemed_approvals(assoc, D(2026, 12, 1)) == 1
    req.refresh_from_db()
    assert (req.status, req.deemed, req.decided_on) == (
        ArcRequest.Status.APPROVED,
        True,
        D(2026, 12, 1),
    )
    assert req.decided_by is None
    letter = Letter.objects.get(pk=req.decision_letter_id)
    assert letter.template_version.template.key == "arc-deemed-approval"
    assert Notification.objects.filter(type_key=deemed.DEEMED).exists()
    # Running again changes nothing.
    assert deemed.record_deemed_approvals(assoc, D(2026, 12, 2)) == 0


def test_request_waiting_on_the_owner_is_not_deemed_approved(assoc, ev_form):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = submit(assoc, p, ev_form)
    with at(D(2026, 11, 25)):
        services.request_info(req, message="Electrician's license?", user=b_usr)
    assert deemed.record_deemed_approvals(assoc, D(2027, 1, 15)) == 0
    req.refresh_from_db()
    assert req.status == ArcRequest.Status.INFO_REQUESTED


def test_board_marks_a_general_request_as_ev_charger_from_receipt(assoc, ev_form):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = submit(assoc, p, services.ensure_default_form(assoc))
    assert req.deny_by is None
    with at(D(2026, 10, 20)):
        deemed.set_kind(req, StatutoryKind.EV_CHARGER, user=b_usr)
    req.refresh_from_db()
    assert req.deny_by == D(2026, 11, 30)


def test_without_the_rule_nothing_is_deemed(assoc):
    _, p = owner(assoc)
    form = services.ensure_default_form(assoc)
    form.statutory_kind = StatutoryKind.HEAT_PUMP
    form.save()
    req = submit(assoc, p, form)
    assert req.deny_by is None
    assert deemed.record_deemed_approvals(assoc, D(2027, 6, 1)) == 0
