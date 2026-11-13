# ruff: noqa: F401, F811 (pytest fixtures imported from test_matching)
"""Reserve withdrawals need two board members (RCW 64.34.372(2) through the rule pack).

The board approves first, then moves the money at the bank; matching the debit uses the
approval. A debit matched with no approval stays on Home until a second board member
approves it after the fact.
"""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa import rules
from homeoa.access.testing import grant
from homeoa.banking import matching, reserve
from homeoa.banking.models import ReserveApproval, TxnStatus
from homeoa.changes import services as changes
from homeoa.changes.models import ChangeRequest
from homeoa.ledger.models import Account

from .test_matching import _import, assoc, operating, units, user
from .test_matching import reserve as reserve_account

D = Decimal
OCT5, OCT6 = dt.date(2026, 10, 5), dt.date(2026, 10, 6)


@pytest.fixture
def two_approvals(assoc):
    rules.set_override(assoc, "reserves.spend_min_approvals", 2, reason="test")


def _transfer_out(operating, reserve_account, amount="1200.00"):
    (out,) = _import(reserve_account, (OCT5, "TRANSFER TO CHECKING", f"-{amount}"))
    (inn,) = _import(operating, (OCT6, "TRANSFER FROM SAVINGS", amount))
    return out, inn


def test_reserve_transfer_uses_an_approval_given_before_the_move(
    two_approvals,
    assoc,
    operating,
    reserve_account,
    units,
    user,
):
    approval = reserve.approve(assoc, amount=D("1200.00"), purpose="Roof repair", user=user)
    assert [i["id"] for i in reserve.todo_items(assoc)] == [f"reserve-move-{approval.pk}"]
    out, inn = _transfer_out(operating, reserve_account)
    matching.confirm_interfund(out, inn, user)
    approval.refresh_from_db()
    assert approval.txn == out
    assert reserve.todo_items(assoc) == []


def test_reserve_withdrawal_matched_without_approval_stays_on_home(
    two_approvals,
    assoc,
    operating,
    reserve_account,
    units,
    user,
):
    # An approval for a different amount is not used.
    reserve.approve(assoc, amount=D("50.00"), purpose="Bank fee", user=user)
    out, inn = _transfer_out(operating, reserve_account)
    matching.confirm_interfund(out, inn, user)
    assert reserve.unapproved(assoc) == [out]
    ids = [i["id"] for i in reserve.todo_items(assoc)]
    assert f"reserve-unapproved-{out.pk}" in ids


def test_categorized_reserve_fee_needs_approval_too(
    two_approvals,
    assoc,
    reserve_account,
    units,
    user,
):
    (fee,) = _import(reserve_account, (OCT5, "SERVICE FEE", "-12.00"))
    matching.categorize(fee, Account.objects.get(code="5400"), user)
    assert reserve.unapproved(assoc) == [fee]


def test_undoing_the_match_frees_the_approval_for_the_next_match(
    two_approvals,
    assoc,
    operating,
    reserve_account,
    units,
    user,
):
    approval = reserve.approve(assoc, amount=D("1200.00"), purpose="Roof repair", user=user)
    out, inn = _transfer_out(operating, reserve_account)
    matching.confirm_interfund(out, inn, user)
    matching.undo(out, user)
    approval.refresh_from_db()
    assert approval.txn is None
    out.refresh_from_db()
    assert out.status == TxnStatus.UNMATCHED
    matching.confirm_interfund(out, inn, user)
    approval.refresh_from_db()
    assert approval.txn == out


def test_operating_debits_and_deposits_need_no_reserve_approval(
    two_approvals,
    assoc,
    operating,
    reserve_account,
    units,
    user,
):
    # Money into reserve, and out of operating, is not a reserve withdrawal.
    (inn,) = _import(reserve_account, (OCT6, "TRANSFER FROM CHECKING", "500.00"))
    (out,) = _import(operating, (OCT5, "TRANSFER TO SAVINGS", "-500.00"))
    matching.confirm_interfund(out, inn, user)
    assert reserve.unapproved(assoc) == []


def test_no_rule_no_reserve_to_dos(assoc, operating, reserve_account, units, user):
    out, inn = _transfer_out(operating, reserve_account)
    matching.confirm_interfund(out, inn, user)
    assert reserve.unapproved(assoc) == []


def _login(client, association, email, **kw):
    u = get_user_model().objects.create_user(email=email)
    grant(u, association=association, **kw)
    client.force_login(u)
    return u


def test_treasurer_asks_and_a_second_board_member_approves(
    two_approvals,
    assoc,
    reserve_account,
    client,
):
    president = get_user_model().objects.create_user(email="p@example.com")
    grant(president, association=assoc, title="president")
    _login(client, assoc, "t@example.com", title="treasurer")
    resp = client.post("/money/bank/reserve/", {"amount": "1200.00", "purpose": "Roof repair"})
    assert resp.status_code == 302
    change = ChangeRequest.objects.get()
    assert change.summary == "Approve a reserve withdrawal: $1200.00"
    assert not ReserveApproval.objects.exists()
    changes.approve(change, president)
    approval = ReserveApproval.objects.get()
    assert (approval.amount, approval.purpose, approval.txn) == (D("1200.00"), "Roof repair", None)


def test_withdrawal_already_made_is_approved_after_the_fact(
    two_approvals,
    assoc,
    operating,
    reserve_account,
    units,
    user,
    client,
):
    out, inn = _transfer_out(operating, reserve_account)
    matching.confirm_interfund(out, inn, user)
    president = get_user_model().objects.create_user(email="p@example.com")
    grant(president, association=assoc, title="president")
    _login(client, assoc, "t@example.com", title="treasurer")
    page = client.get(f"/money/bank/reserve/?txn={out.pk}")
    assert page.status_code == 200
    assert b"Approve a withdrawal already made" in page.content
    client.post("/money/bank/reserve/", {"txn": out.pk, "purpose": "Emergency gutter repair"})
    change = ChangeRequest.objects.get()
    changes.approve(change, president)
    approval = ReserveApproval.objects.get()
    assert (approval.txn, approval.after_the_fact, approval.amount) == (out, True, D("1200.00"))
    assert reserve.unapproved(assoc) == []
