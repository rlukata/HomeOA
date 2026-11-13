"""Non-binding owner polls: one answer per unit, results to the board, never a vote."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import open_residents
from homeoa.community import polls
from homeoa.community.models import Poll, PollAnswer
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.plugins import registry as plugin_registry
from homeoa.registry.models import Unit

User = get_user_model()
TODAY = dt.date(2026, 10, 7)
BOARD = "treasurer@cedarrow.example"
OWNER_C = "ownerc@cedarrow.example"
TENANT_E = "chris.doyle@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


def _poll(client, **extra):
    client.force_login(User.objects.get(email=BOARD))
    data = {
        "question": "Repaint the doors blue?",
        "choices": "Yes\nNo\nNo opinion",
        "opens_on": "2026-10-07",
        "closes_on": "2026-10-20",
        "show_results": "on",
        **extra,
    }
    assert client.post(reverse("community:polls"), data).status_code == 302
    return Poll.objects.get()


def test_owner_answers_once_per_unit_and_can_change(pilot, client):
    poll = _poll(client)
    client.force_login(User.objects.get(email=OWNER_C))
    assert "Repaint the doors blue?" in client.get(reverse("portal:home")).content.decode()
    url = reverse("community:resident_poll", args=[poll.pk])
    client.post(url, {"unit": "C", "choice": "0"})
    client.post(url, {"unit": "C", "choice": "1"})
    (a,) = PollAnswer.objects.all()
    assert a.unit.number == "C" and a.choice == 1
    r = polls.results(poll)
    assert r["answered"] == 1 and r["rows"][1][1] == 1 and r["units"] == 9


def test_tenants_only_when_the_board_includes_them(pilot, client):
    poll = _poll(client)
    tenant = User.objects.get(email=TENANT_E)
    assert polls.units_for(tenant, poll, TODAY) == []
    poll.include_tenants = True
    poll.save()
    assert [u.number for u in polls.units_for(tenant, poll, TODAY)] == ["E"]


def test_closed_poll_takes_no_answers_and_shows_results(pilot, client):
    poll = _poll(client)
    client.post(reverse("community:poll_close", args=[poll.pk]))
    poll.refresh_from_db()
    assert not poll.is_open(TODAY)
    client.force_login(User.objects.get(email=OWNER_C))
    owner = User.objects.get(email=OWNER_C)
    with pytest.raises(Exception, match="closed"):
        polls.answer(
            poll, user=owner, person=None, unit=Unit.objects.get(number="C"), choice=0, on=TODAY
        )


def test_needs_two_choices(pilot, client):
    client.force_login(User.objects.get(email=BOARD))
    resp = client.post(
        reverse("community:polls"),
        {
            "question": "Q",
            "choices": "Only one",
            "opens_on": "2026-10-07",
            "closes_on": "2026-10-08",
        },
    )
    assert resp.status_code == 400 and not Poll.objects.exists()
