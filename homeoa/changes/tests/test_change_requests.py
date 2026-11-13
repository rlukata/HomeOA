"""Change requests: who approves, how many approvals, rejection, withdrawal, failures."""

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError

from homeoa.access.testing import grant
from homeoa.changes import kinds, services
from homeoa.changes.models import ChangeRequest
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos

User = get_user_model()
applied = []


def _apply(change):
    if change.payload.get("fail"):
        applied.append("partial")
        raise ValidationError("The vendor name is already taken.")
    applied.append(change.payload["value"])
    return "/done/"


kinds.register(kinds.Kind("test.thing", "Test thing", _apply))


@pytest.fixture(autouse=True)
def _clear():
    applied.clear()


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    with association_context(a):
        yield a


def _board(assoc, *emails):
    out = []
    for email in emails:
        u = User.objects.create_user(email)
        grant(u, association=assoc)
        out.append(u)
    return out


def _propose(assoc, user, **payload):
    payload.setdefault("value", "x")
    return services.propose(assoc, "test.thing", payload, user=user, summary="Do a thing")


def test_board_proposal_needs_one_other_board_member(assoc):
    a, b, c = _board(assoc, "a@example.org", "b@example.org", "c@example.org")
    change = _propose(assoc, a)
    assert change.required_approvals == 1 and change.is_pending and applied == []
    assert not services.can_decide(change, a)  # nobody approves their own
    with pytest.raises(PermissionDenied):
        services.approve(change, a)
    services.approve(change, b)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.APPLIED and applied == ["x"]
    assert change.result_url == "/done/"
    assert not services.can_decide(change, c)


def test_non_board_proposal_needs_two_board_members(assoc):
    a, b = _board(assoc, "a@example.org", "b@example.org")
    committee = User.objects.create_user("m@example.org")
    grant(committee, "maintenance_committee", association=assoc)
    change = _propose(assoc, committee)
    assert change.required_approvals == 2
    services.approve(change, a)
    change.refresh_from_db()
    assert change.is_pending and applied == []
    with pytest.raises(PermissionDenied):
        services.approve(change, a)  # one decision each
    services.approve(change, b)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.APPLIED


def test_only_board_members_decide(assoc):
    a, _b = _board(assoc, "a@example.org", "b@example.org")
    outsider = User.objects.create_user("o@example.org")
    change = _propose(assoc, a)
    with pytest.raises(PermissionDenied):
        services.approve(change, outsider)


def test_rejection_closes_without_changing_anything(assoc):
    a, b, c = _board(assoc, "a@example.org", "b@example.org", "c@example.org")
    change = _propose(assoc, a)
    with pytest.raises(services.ChangeError):
        services.reject(change, b, "")
    services.reject(change, b, "Wrong vendor")
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.REJECTED and applied == []
    with pytest.raises(PermissionDenied):
        services.approve(change, c)


def test_withdraw_only_by_proposer(assoc):
    a, b = _board(assoc, "a@example.org", "b@example.org")
    change = _propose(assoc, a)
    with pytest.raises(PermissionDenied):
        services.withdraw(change, b)
    services.withdraw(change, a)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.WITHDRAWN and applied == []


def test_failed_change_keeps_nothing_and_says_why(assoc):
    a, b = _board(assoc, "a@example.org", "b@example.org")
    change = _propose(assoc, a, fail=True)
    services.approve(change, b)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.FAILED
    assert "already taken" in change.error


def test_small_boards_never_deadlock(assoc):
    (solo,) = _board(assoc, "solo@example.org")
    change = _propose(assoc, solo)
    assert change.status == ChangeRequest.Status.APPLIED  # nobody else could approve
    committee = User.objects.create_user("m@example.org")
    change = _propose(assoc, committee)
    assert change.required_approvals == 1  # the whole (one-person) board
    services.approve(change, solo)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.APPLIED


def test_each_approver_gets_a_home_todo(assoc):
    a, b, c = _board(assoc, "a@example.org", "b@example.org", "c@example.org")
    change = _propose(assoc, a)
    mine = {
        i.for_user for i in todos.collect(assoc) if i.id.startswith(f"change-approve-{change.pk}-")
    }
    assert mine == {b.pk, c.pk}
    services.approve(change, b)
    assert not [i for i in todos.collect(assoc) if i.id.startswith("change-approve-")]


def test_pages_show_board_everything_and_others_their_own(client, assoc):
    a, b = _board(assoc, "a@example.org", "b@example.org")
    committee = User.objects.create_user("m@example.org")
    mine = _propose(assoc, committee)
    board_only = _propose(assoc, a)
    client.force_login(committee)
    assert client.get(f"/governance/changes/{mine.pk}/").status_code == 200
    assert client.get(f"/governance/changes/{board_only.pk}/").status_code == 404
    client.force_login(b)
    html = client.get("/governance/changes/").content.decode()
    assert "Waiting for your approval" in html
    resp = client.post(f"/governance/changes/{board_only.pk}/approve/", follow=True)
    assert "The change has been made" in resp.content.decode()
