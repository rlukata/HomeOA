"""While the association is setting up, a new dues schedule and a new late-fee policy start
on the go-live date by default (dues: the first 1st on or after it)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant, make_live
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts

TODAY = dt.date(2026, 10, 7)
SCHEDULE_NEW = "/money/assessments/schedules/new/"
POLICY_NEW = "/money/assessments/late-fees/policies/new/"


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def board(assoc, client):
    user = get_user_model().objects.create_user(email="board@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


def _start(client, url):
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.context["form"]["effective_start"].value()


def test_new_dues_schedule_starts_on_go_live_when_it_is_a_first(assoc, board):
    make_live(assoc, since=dt.date(2026, 11, 1))
    assert _start(board, SCHEDULE_NEW) == dt.date(2026, 11, 1)


def test_new_dues_schedule_starts_on_the_first_after_a_mid_month_go_live(assoc, board):
    make_live(assoc, since=dt.date(2026, 11, 15))
    assert _start(board, SCHEDULE_NEW) == dt.date(2026, 12, 1)


def test_new_late_fee_policy_starts_on_go_live(assoc, board):
    make_live(assoc, since=dt.date(2026, 11, 15))
    assert _start(board, POLICY_NEW) == dt.date(2026, 11, 15)


def test_live_association_keeps_the_usual_defaults(assoc, board):
    make_live(assoc, since=dt.date(2026, 8, 15))
    assert _start(board, SCHEDULE_NEW) == dt.date(2026, 10, 1)
    assert _start(board, POLICY_NEW) == TODAY


def test_without_a_go_live_date_the_usual_defaults_stay(assoc, board):
    assert _start(board, SCHEDULE_NEW) == dt.date(2026, 10, 1)
    assert _start(board, POLICY_NEW) == TODAY
