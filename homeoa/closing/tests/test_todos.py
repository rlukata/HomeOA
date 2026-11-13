"""The "Close <Month YYYY>" Home to-do."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant
from homeoa.closing import services
from homeoa.closing import todos as close_todos
from homeoa.closing.tests.test_closing import assoc, post, reconcile, user  # noqa: F401
from homeoa.dashboard import todos

pytestmark = pytest.mark.django_db


@pytest.fixture
def today(monkeypatch):
    box = {"d": dt.date(2026, 1, 20)}
    monkeypatch.setattr(close_todos, "association_today", lambda a=None: box["d"])
    return box


def _close_items(assoc, u=None):  # noqa: F811
    return [i for i in todos.collect(assoc, u) if i.source == "closing"]


def test_absent_with_empty_ledger(assoc, today):  # noqa: F811
    assert close_todos.todo_items(assoc) == []


def test_absent_before_month_end(assoc, today):  # noqa: F811
    post("1000", "4000", "100.00", dt.date(2026, 1, 5))
    today["d"] = dt.date(2026, 1, 31)
    assert close_todos.todo_items(assoc) == []


def test_failing_then_ready_then_gone(assoc, user, today):  # noqa: F811
    post("1000", "4000", "100.00", dt.date(2026, 1, 5))
    reconcile(dt.date(2025, 12, 31))
    today["d"] = dt.date(2026, 2, 1)
    [item] = _close_items(assoc, user)
    assert item.title == "Close January 2026"
    assert "Bank reconciliations done" in item.sub
    assert item.url.endswith("?month=2026-01")
    reconcile(dt.date(2026, 1, 31))
    [item] = _close_items(assoc, user)
    assert item.sub.startswith("Ready")
    services.close_month(assoc, 2026, 1, user=user)
    assert _close_items(assoc, user) == []  # February has not ended yet
    today["d"] = dt.date(2026, 4, 2)
    [item] = _close_items(assoc, user)  # only the oldest unclosed month
    assert item.title == "Close February 2026"


def test_hidden_without_finance_manage(assoc, user, today):  # noqa: F811
    post("1000", "4000", "100.00", dt.date(2026, 1, 5))
    today["d"] = dt.date(2026, 2, 1)
    member = get_user_model().objects.create_user("board@example.com", password="x")
    grant(member, association=assoc)  # board, no treasurer title
    owner = get_user_model().objects.create_user("owner@example.com", password="x")
    assert _close_items(assoc, member) == []
    assert _close_items(assoc, owner) == []
    assert len(_close_items(assoc, user)) == 1
