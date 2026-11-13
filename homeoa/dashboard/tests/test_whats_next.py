"""The Home "what's next" card lists the setup steps, ticked from existing facts."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.models import Invitation
from homeoa.access.permissions import SYS_ADMIN
from homeoa.access.testing import grant
from homeoa.billing.models import AssessmentSchedule
from homeoa.billing.services import default_schedule_kwargs
from homeoa.changes.models import ChangeRequest
from homeoa.core import status
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import whats_next
from homeoa.governance.models import BoardTerm
from homeoa.imports.models import ImportRun
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership
from homeoa.setup import services as setup

TODAY = dt.date(2026, 10, 15)
START = dt.date(2026, 1, 1)
User = get_user_model()


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Test HOA", short_name="T")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def board_user(assoc):
    user = User.objects.create_user(email="board@example.com")
    grant(user, association=assoc, title="president", start=START)
    return user


@pytest.fixture
def admin_user(assoc):
    user = User.objects.create_user(email="admin@example.com")
    grant(user, SYS_ADMIN, association=assoc, start=START)
    return user


@pytest.fixture
def fixed_today(monkeypatch):
    from homeoa.dashboard import views
    from homeoa.registry import services as reg

    monkeypatch.setattr(views, "association_today", lambda a=None: TODAY)
    monkeypatch.setattr(reg, "today_for", lambda a=None: TODAY)
    return TODAY


def _steps(assoc, user):
    return {s.key: s for s in whats_next.steps(assoc, user, TODAY)}


def _next(assoc, user):
    return next(s.key for s in whats_next.steps(assoc, user, TODAY) if s.next)


def _seat_board(assoc):
    person = Person.objects.create(association=assoc, full_name="Pat President")
    BoardTerm.objects.create(association=assoc, person=person, office="president", start=START)


def _adopt(assoc, user, key):
    ChangeRequest.objects.create(
        association=assoc,
        kind=setup.KIND,
        summary=key,
        payload={"section": key},
        proposed_by=user,
        status=ChangeRequest.Status.APPLIED,
    )


def _adopt_all(assoc, user):
    for s in setup.sections(assoc):
        _adopt(assoc, user, s.key)


def _unit(assoc, number="A", owner_email="owner@example.com"):
    unit = Unit.objects.create(
        association=assoc, number=number, allocated_interest=Decimal("1"), vote_weight=1
    )
    if owner_email is not None:
        person = Person.objects.create(association=assoc, full_name="Owner", email=owner_email)
        set_initial_ownership(unit, [person], START)
    return unit


def _dues(assoc):
    AssessmentSchedule.objects.create(
        association=assoc,
        name="Dues",
        effective_start=TODAY.replace(day=1),
        **default_schedule_kwargs(assoc),
    )


def _go_live(assoc):
    status.set_go_live_date(assoc, TODAY, reason="test")


def _owner_with_email(assoc):
    return Person.objects.get(association=assoc, email="owner@example.com")


def _invite(assoc, person):
    Invitation.objects.create(
        association=assoc, person=person, email=person.email, status=Invitation.Status.SENT
    )


def _finish_everything(assoc, user):
    _seat_board(assoc)
    _adopt_all(assoc, user)
    _unit(assoc)
    _dues(assoc)
    _go_live(assoc)
    _invite(assoc, _owner_with_email(assoc))


def test_a_new_association_lists_every_step_with_board_added_next(assoc, board_user):
    steps = whats_next.steps(assoc, board_user, TODAY)
    assert [s.key for s in steps] == [
        "board",
        "features",
        "policies",
        "units",
        "dues",
        "go_live",
        "invite",
    ]
    assert _next(assoc, board_user) == "board"


def test_board_added_ticks_once_the_association_has_a_current_board(assoc, board_user):
    _seat_board(assoc)
    assert _steps(assoc, board_user)["board"].done
    assert _next(assoc, board_user) == "features"


def test_features_and_policies_tick_from_adopted_setup_sections(assoc, board_user):
    _adopt(assoc, board_user, setup.FEATURES)
    steps = _steps(assoc, board_user)
    assert steps["features"].done
    assert not steps["policies"].done
    _adopt_all(assoc, board_user)
    assert _steps(assoc, board_user)["policies"].done


def test_units_and_owners_tick_only_when_every_unit_has_an_owner(assoc, board_user):
    _unit(assoc, "A")
    _unit(assoc, "B", owner_email=None)
    assert not _steps(assoc, board_user)["units"].done
    Unit.objects.get(number="B").delete()
    assert _steps(assoc, board_user)["units"].done


def test_dues_tick_from_an_active_assessment_schedule(assoc, board_user):
    _dues(assoc)
    assert _steps(assoc, board_user)["dues"].done
    AssessmentSchedule.objects.update(active=False)
    assert not _steps(assoc, board_user)["dues"].done


def test_records_step_shows_only_once_an_import_exists_and_ticks_when_committed(assoc, board_user):
    assert "records" not in _steps(assoc, board_user)
    run = ImportRun.objects.create(association=assoc, source="csv", as_of=TODAY)
    assert not _steps(assoc, board_user)["records"].done
    run.status = ImportRun.Status.COMMITTED
    run.save()
    assert _steps(assoc, board_user)["records"].done


def test_go_live_links_to_the_go_live_page_only_for_the_system_admin(assoc, board_user, admin_user):
    assert _steps(assoc, admin_user)["go_live"].url.endswith("/go-live/")
    board_step = _steps(assoc, board_user)["go_live"]
    assert board_step.url == ""
    assert "System admin" in board_step.note
    _go_live(assoc)
    assert _steps(assoc, board_user)["go_live"].done


def test_owners_invited_ticks_when_every_owner_with_an_email_is_invited_or_has_an_account(
    assoc, board_user
):
    _unit(assoc, "A")
    _unit(assoc, "B", owner_email="b@example.com")
    _unit(assoc, "C", owner_email="")
    assert not _steps(assoc, board_user)["invite"].done
    _invite(assoc, _owner_with_email(assoc))
    assert not _steps(assoc, board_user)["invite"].done
    b = Person.objects.get(email="b@example.com")
    b.user = User.objects.create_user(email="b@example.com")
    b.save()
    assert _steps(assoc, board_user)["invite"].done


def test_the_card_is_gone_once_every_step_is_done(assoc, board_user):
    _finish_everything(assoc, board_user)
    assert whats_next.pending_steps(assoc, board_user, TODAY) == []


def test_home_shows_the_card_and_hides_the_setup_to_do_while_it_shows(
    assoc, board_user, client, fixed_today
):
    client.force_login(board_user)
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'data-testid="whats-next"' in body
    assert "Board added" in body
    assert 'aria-current="step"' in body
    assert setup.todo_items(assoc), "the setup to-do exists but Home leaves it out"
    assert "Finish association setup" not in body


def test_a_user_without_board_access_does_not_see_the_card(assoc, client, fixed_today):
    cpa = User.objects.create_user(email="cpa@example.com")
    grant(cpa, "cpa", association=assoc, start=START)
    client.force_login(cpa)
    body = client.get("/").content.decode()
    assert 'data-testid="whats-next"' not in body
