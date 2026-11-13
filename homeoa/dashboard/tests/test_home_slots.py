"""Home to-dos and Home cards come from the HOME_TODOS and HOME_CARDS slots."""

import datetime as dt

import pytest

from homeoa.access.testing import grant
from homeoa.dashboard import todos
from homeoa.maintenance import services as maintenance
from homeoa.maintenance.tests.test_maintenance import User, _unit, assoc  # noqa: F401
from homeoa.plugins import slots
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

TODAY = dt.date(2026, 10, 15)

MODULE_TODOS = {
    "autopay": "online_payments",
    "enforcement": "violations",
    "arc": "arc",
    "owner_requests": "owner_requests",
    "work_orders": "work_orders",
}


def _todo_contributions():
    return {c.value[0]: c.module for c in slots.contributions(slots.HOME_TODOS)}


def test_each_module_to_do_provider_is_tagged_with_its_module():
    found = _todo_contributions()
    for name, module in MODULE_TODOS.items():
        assert found[name] == module


def test_core_to_do_providers_are_tagged_core_and_votes_stay_core():
    found = _todo_contributions()
    for name in todos.CORE_PROVIDERS:
        assert found[name] is None
    assert found["votes"] is None


def test_core_does_not_name_module_to_do_providers():
    paths = " ".join(todos.CORE_PROVIDERS.values())
    for package in ("enforcement", "arc", "maintenance", "autopay", "community"):
        assert package not in paths


def test_to_do_providers_keep_their_home_order():
    names = [name for name, _provider in todos.providers(None)]
    expected = [
        "banking",
        "ops",
        "parallel_run",
        "notify",
        "delinquency",
        "interest",
        "autopay",
        "compliance",
        "vendors",
        "bills",
        "changes",
        "interfund",
        "reserve",
        "setup",
        "records",
        "resale",
        "closing",
        "governance",
        "enforcement",
        "votes",
        "meetings",
        "annual_meeting",
        "esign",
        "arc",
        "owner_requests",
        "work_orders",
    ]
    assert [n for n in names if n in expected] == expected


def test_home_cards_slot_holds_core_cards_and_the_calendar_module_card():
    found = [(c.value, c.module) for c in slots.contributions(slots.HOME_CARDS)]
    assert found[:4] == [
        ("dashboard/_home_whats_next.html", None),
        ("dashboard/_home_money_cards.html", None),
        ("community/_home_calendar_card.html", "calendar"),
        ("dashboard/_home_activity.html", None),
    ]


def _item(prefix, title, due):
    def provider(association):
        return [{"id": f"{prefix}-1", "title": title, "due": due, "kind": "gov"}]

    return provider


@pytest.fixture
def home(assoc, client, monkeypatch):  # noqa: F811
    from homeoa.dashboard import views
    from homeoa.registry import services as reg

    monkeypatch.setattr(views, "association_today", lambda a=None: TODAY)
    monkeypatch.setattr(reg, "today_for", lambda a=None: TODAY)
    monkeypatch.setattr(maintenance, "association_today", lambda a=None: TODAY)
    user = User.objects.create_user("home-board@example.org")
    grant(user, association=assoc, start=dt.date(2020, 1, 1))
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_board_home_lists_every_module_to_do_in_due_date_order(home, assoc, monkeypatch):  # noqa: F811
    from homeoa.arc import todos as arc_todos
    from homeoa.enforcement import todos as enforcement_todos
    from homeoa.payments import autopay
    from homeoa.votes import todos as vote_todos

    monkeypatch.setattr(autopay, "todo_items", _item("ap", "Autopay failed", TODAY))
    monkeypatch.setattr(
        enforcement_todos, "todo_items", _item("enf", "Record hearing decision", TODAY)
    )
    monkeypatch.setattr(arc_todos, "todo_items", _item("arc", "ARC decision overdue", TODAY))
    monkeypatch.setattr(vote_todos, "todo_items", _item("vote", "Count the vote", None))
    req = maintenance.submit_request(title="Gutter leak", unit=_unit("I"))
    vendor = vendor_services.save_vendor(Vendor(name="Evergreen Gutters"))
    wo = maintenance.create_work_order(vendor=vendor, scope="x", scheduled_for=dt.date(2026, 10, 1))
    maintenance.set_work_order_status(wo, "scheduled")

    resp = home.get("/")
    titles = [i.title for g in resp.context["todo_groups"] for i in g["items"]]
    wanted = [
        "Count the vote",
        f"Triage maintenance request: {req.title}",
        f"Work order {wo.pk} is past its scheduled date",
        "ARC decision overdue",
        "Autopay failed",
        "Record hearing decision",
    ]
    assert [t for t in titles if t in wanted] == wanted
    sources = {i.title: i.source for g in resp.context["todo_groups"] for i in g["items"]}
    assert sources[wanted[1]] == "owner_requests"
    assert sources[wanted[2]] == "work_orders"


@pytest.mark.django_db
def test_board_home_shows_the_calendar_card_between_money_and_activity(home):
    page = home.get("/").content.decode()
    calendar = page.index('data-testid="home-calendar"')
    assert calendar < page.index('id="activity-h"')
    assert page.count('data-testid="home-calendar"') == 1
