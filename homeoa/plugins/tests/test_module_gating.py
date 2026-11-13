"""A module that is off disappears everywhere core reaches it: slots, pages (board and
owner portal), the scheduler, event dispatch, core links into it and resident
notification preferences."""

import datetime as dt
from decimal import Decimal

import pytest

from homeoa import events
from homeoa.core.models import Association
from homeoa.notify import types as ntypes
from homeoa.plugins import modules, registry, slots
from homeoa.portal.tests.test_requests_tab import BOARD, OWNER_A, _login, _on
from homeoa.portal.tests.test_requests_tab import pilot as pilot  # noqa: F401
from homeoa.ui.nav import SubTab, register_subtab


@pytest.fixture
def fresh_slots(monkeypatch):
    """Contributions added by a test are dropped afterwards."""
    monkeypatch.setattr(
        slots, "_contributions", {k: list(v) for k, v in slots._contributions.items()}
    )


def _off(association, module_id):
    modules.set_module(association, module_id, False, user=None)


def _reload(association):
    return Association.objects.get(pk=association.pk)


# --- slots -----------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_slots")
def test_slot_leaves_out_a_switched_off_module_for_that_association_only(db):
    here = Association.objects.create(name="Here", short_name="H")
    there = Association.objects.create(name="There", short_name="T")
    slots.register(slots.HOME_CARDS, "core.html", order=-99)
    slots.register(slots.HOME_CARDS, "polls.html", module="polls", order=-98)
    _off(here, "polls")

    assert "polls.html" not in slots.values(slots.HOME_CARDS, _reload(here))
    assert "core.html" in slots.values(slots.HOME_CARDS, _reload(here))
    assert "polls.html" in slots.values(slots.HOME_CARDS, _reload(there))
    # An association id (as events carry) is answered the same way.
    assert "polls.html" not in slots.values(slots.HOME_CARDS, here.pk)
    # Without an association every contribution is listed.
    assert "polls.html" in slots.values(slots.HOME_CARDS)


def test_module_on_answers_for_the_association_and_always_for_core(db):
    a = Association.objects.create(name="Here", short_name="H")
    _off(a, "calendar")
    assert slots.module_on("calendar", _reload(a)) is False
    assert slots.module_on("calendar", a.pk) is False
    assert slots.module_on("polls", _reload(a)) is True
    assert slots.module_on(None, a) is True
    assert slots.module_on("calendar", None) is True


# --- pages -----------------------------------------------------------------------------


def test_violations_board_and_owner_pages_404_while_violations_is_off(client, pilot):
    _login(client, BOARD)
    assert client.get("/requests/violations/").status_code == 200
    _off(pilot, "violations")
    assert client.get("/requests/violations/").status_code == 404
    assert client.post("/requests/violations/add/").status_code == 404
    assert client.get("/me/violations/1/").status_code == 404


def test_calendar_page_404s_while_calendar_is_off_and_announcements_stay(client, pilot):
    _login(client, BOARD)
    assert client.get("/community/calendar/").status_code == 200
    _off(pilot, "calendar")
    assert client.get("/community/calendar/").status_code == 404
    assert client.get("/community/announcements/").status_code == 200


def test_resident_community_page_404s_only_when_announcements_and_calendar_are_off(client, pilot):
    _on(pilot)
    _login(client, OWNER_A)
    _off(pilot, "calendar")
    assert client.get("/community/me/").status_code == 200
    _off(pilot, "announcements")
    assert client.get("/community/me/").status_code == 404


def test_owner_maintenance_pages_404_while_owner_requests_is_off(client, pilot):
    _on(pilot)
    _login(client, OWNER_A)
    assert client.get("/me/maintenance/").status_code == 200
    _off(pilot, "owner_requests")
    assert client.get("/me/maintenance/").status_code == 404
    assert client.get("/me/maintenance/new/").status_code == 404


def test_pay_now_and_autopay_404_while_online_payments_is_off(client, pilot):
    _on(pilot)
    _login(client, OWNER_A)
    assert client.get("/me/pay/confirm/").status_code != 404
    _off(pilot, "online_payments")
    assert client.get("/me/pay/confirm/").status_code == 404
    assert client.post("/me/pay/confirm/").status_code == 404
    assert client.get("/me/pay/autopay/").status_code == 404
    # The Pay page itself stays; it no longer offers to pay online.
    response = client.get("/me/pay/")
    assert response.status_code == 200
    assert b"/me/pay/confirm/" not in response.content


@pytest.mark.usefixtures("fresh_slots")
def test_a_placeholder_section_of_a_switched_off_module_404s(client, pilot):
    register_subtab("community", SubTab("noticeboard", "Noticeboard"), module="polls")
    _login(client, BOARD)
    assert client.get("/community/noticeboard/").status_code == 200
    _off(pilot, "polls")
    assert client.get("/community/noticeboard/").status_code == 404


def test_menu_leaves_out_the_sections_of_a_switched_off_module(client, pilot):
    _login(client, BOARD)
    assert b'href="/community/calendar/"' in client.get("/community/announcements/").content
    _off(pilot, "calendar")
    assert b'href="/community/calendar/"' not in client.get("/community/announcements/").content


# --- core links into a module --------------------------------------------------------


def test_reports_page_drops_the_board_packet_link_while_board_packet_is_off(client, pilot):
    _login(client, BOARD)
    assert b'data-testid="board-packet-link"' in client.get("/money/reports/").content
    _off(pilot, "board_packet")
    assert b'data-testid="board-packet-link"' not in client.get("/money/reports/").content


def test_payments_page_drops_the_autopay_link_while_online_payments_is_off(client, pilot):
    _login(client, BOARD)
    assert b'data-testid="autopay-settings-link"' in client.get("/money/payments/").content
    _off(pilot, "online_payments")
    assert b'data-testid="autopay-settings-link"' not in client.get("/money/payments/").content


# --- scheduler -------------------------------------------------------------------------

ran = []


@pytest.fixture
def module_job(fresh_slots):
    from homeoa.jobs import scheduler

    saved = dict(scheduler._registry)
    scheduler._registry.clear()
    ran.clear()

    def record(association, as_of):
        ran.append((association.pk, as_of))

    scheduler.periodic("t.polls_daily", "0 6 * * *", module="polls")(record)
    yield
    scheduler._registry.clear()
    scheduler._registry.update(saved)


def _assoc(name):
    a = Association.objects.create(name=name, short_name=name[:2], timezone="UTC")
    Association.objects.filter(pk=a.pk).update(created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    return _reload(a)


@pytest.mark.usefixtures("module_job")
def test_scheduler_skips_a_module_job_where_the_module_is_off(db):
    from homeoa.jobs import run_due

    off, on = _assoc("Off"), _assoc("On")
    _off(off, "polls")
    run_due(as_of=dt.date(2026, 9, 11))
    assert ran == [(on.pk, dt.date(2026, 9, 11))]


@pytest.mark.usefixtures("module_job")
def test_turning_a_module_back_on_does_not_replay_the_days_it_was_off(db):
    from homeoa.jobs import run_due
    from homeoa.plugins.models import ModuleSwitch

    a = _assoc("Back")
    tick = dt.datetime(2026, 9, 10, 7, tzinfo=dt.UTC)
    run_due(now=tick)  # catches up to Sep 10 while on
    _off(a, "polls")
    run_due(now=tick + dt.timedelta(days=3))  # off: nothing runs Sep 11-13
    ran.clear()
    modules.set_module(_reload(a), "polls", True, user=None)
    ModuleSwitch.all_associations.filter(association=a, module_id="polls").update(
        changed_at=dt.datetime(2026, 9, 14, 5, tzinfo=dt.UTC)
    )
    run_due(now=tick + dt.timedelta(days=4))
    assert ran == [(a.pk, dt.date(2026, 9, 14))]


# --- events ----------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_slots")
def test_event_handler_of_a_switched_off_module_is_not_called(db):
    from homeoa.events.tests.test_bus import WidgetSold

    off, on = _assoc("Off"), _assoc("On")
    _off(off, "polls")
    seen = []
    events.handle("test.widget_sold", seen.append, module="polls")

    def sold(association_id):
        return WidgetSold(
            association_id=association_id,
            widget="w",
            amount=Decimal("1.00"),
            sold_on=dt.date(2026, 1, 1),
        )

    events.publish(sold(off.pk))
    assert seen == []
    events.publish(sold(on.pk))
    assert [e.association_id for e in seen] == [on.pk]


# --- notification preferences ----------------------------------------------------------


def test_resident_preferences_leave_out_a_switched_off_module_notification(client, pilot):
    _on(pilot)
    _login(client, OWNER_A)
    label = str(ntypes.get("community.announcement").label).encode()
    assert label in client.get("/me/profile/").content
    _off(pilot, "announcements")
    assert label not in client.get("/me/profile/").content


# --- demo ------------------------------------------------------------------------------


def test_seed_demo_turns_on_every_module_for_the_demo_association(pilot):
    from homeoa.plugins.models import ModuleSwitch

    rows = dict(
        ModuleSwitch.all_associations.filter(association=pilot).values_list("module_id", "enabled")
    )
    assert rows == {m.id: True for m in registry.all_modules()}


# --- switch timing ---------------------------------------------------------------------


def test_setting_a_module_to_the_state_it_already_has_changes_nothing(db):
    from homeoa.audit.models import AuditEvent
    from homeoa.plugins.models import ModuleSwitch

    a = Association.objects.create(name="Same", short_name="S")
    modules.set_module(a, "polls", True, user=None)
    before = ModuleSwitch.all_associations.get(association=a, module_id="polls").changed_at
    events_before = AuditEvent.objects.count()
    modules.set_module(_reload(a), "polls", True, user=None)
    assert ModuleSwitch.all_associations.get(association=a, module_id="polls").changed_at == before
    assert AuditEvent.objects.count() == events_before


@pytest.mark.usefixtures("module_job")
def test_a_module_on_since_before_a_gap_still_catches_up_the_missed_days(db):
    from homeoa.jobs import run_due
    from homeoa.plugins.models import ModuleSwitch

    a = _assoc("Gap")
    modules.set_module(a, "polls", True, user=None)
    ModuleSwitch.all_associations.filter(association=a, module_id="polls").update(
        changed_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    )
    tick = dt.datetime(2026, 9, 10, 7, tzinfo=dt.UTC)
    run_due(now=tick)
    ran.clear()
    run_due(now=tick + dt.timedelta(days=3))  # the worker was down Sep 11-12
    assert [d for _pk, d in ran] == [dt.date(2026, 9, d) for d in (11, 12, 13)]


# --- parts of shared pages -------------------------------------------------------------


def test_maintenance_list_shows_only_the_parts_whose_module_is_on(client, pilot):
    _login(client, BOARD)
    html = client.get("/requests/maintenance/").content.decode()
    assert 'id="req-h"' in html and 'id="wo-h"' in html
    _off(pilot, "work_orders")
    html = client.get("/requests/maintenance/").content.decode()
    assert 'id="req-h"' in html and 'id="wo-h"' not in html
    modules.set_module(_reload(pilot), "work_orders", True, user=None)
    _off(_reload(pilot), "owner_requests")
    html = client.get("/requests/maintenance/").content.decode()
    assert 'id="req-h"' not in html and 'id="wo-h"' in html
    assert 'data-testid="maint-add"' not in html


def test_resident_community_page_shows_only_the_parts_whose_module_is_on(client, pilot):
    _on(pilot)
    _login(client, OWNER_A)
    html = client.get("/community/me/").content.decode()
    assert 'data-testid="announcements"' in html and 'data-testid="calendar"' in html
    _off(pilot, "calendar")
    html = client.get("/community/me/").content.decode()
    assert 'data-testid="announcements"' in html and 'data-testid="calendar"' not in html
    modules.set_module(_reload(pilot), "calendar", True, user=None)
    _off(_reload(pilot), "announcements")
    html = client.get("/community/me/").content.decode()
    assert 'data-testid="announcements"' not in html and 'data-testid="calendar"' in html


# --- more core links -------------------------------------------------------------------


def test_bill_page_drops_the_work_order_link_while_work_orders_is_off(client, pilot):
    from homeoa.ap.models import Bill
    from homeoa.maintenance import services as maint
    from homeoa.vendors import services as vendor_services
    from homeoa.vendors.models import Vendor

    vendor = vendor_services.save_vendor(Vendor(name="Evergreen Gutters"))
    wo = maint.create_work_order(vendor=vendor, scope="Clean gutters")
    bill = Bill.objects.create(
        vendor=vendor,
        invoice_number="EG-1",
        bill_date=dt.date(2026, 9, 30),
        due_date=dt.date(2026, 10, 22),
        description="Gutter cleaning",
    )
    maint.link_bill(wo, bill)
    _login(client, BOARD)
    url = f"/money/bills/{bill.pk}/"
    assert b'data-testid="bill-work-order"' in client.get(url).content
    wo.status = wo.Status.COMPLETED  # an open work order stops the module turning off
    wo.save()
    _off(pilot, "work_orders")
    assert b'data-testid="bill-work-order"' not in client.get(url).content


def test_board_page_drops_the_committees_card_while_committees_is_off(client, pilot):
    _login(client, BOARD)
    assert b'data-testid="committees"' in client.get("/governance/board/").content
    _off(pilot, "arc")  # design approval requires committees
    _off(_reload(pilot), "committees")
    assert b'data-testid="committees"' not in client.get("/governance/board/").content


# --- letter merge fields ---------------------------------------------------------------


def test_merge_fields_of_a_switched_off_module_are_left_out(db):
    from homeoa.letters import merge

    a = Association.objects.create(name="Merge", short_name="M")
    assert "violation.cure_by" in merge.fields(a)
    _off(a, "violations")
    assert "violation.cure_by" not in merge.fields(_reload(a))
    assert any(k.startswith("arc.") for k in merge.fields(_reload(a)))
    assert "violation.cure_by" in merge.fields()
