"""Each optional module reports what it still has in flight, for the turn-off check."""

import datetime as dt
from decimal import Decimal

import pytest

from homeoa.arc.models import ArcForm, ArcRequest
from homeoa.community.models import Announcement, Poll
from homeoa.core.models import Association
from homeoa.enforcement.models import Violation, ViolationCategory
from homeoa.maintenance.models import MaintenanceRequest, WorkOrder
from homeoa.payments.models import AutopayCharge, AutopayEnrollment, AutopayStatus
from homeoa.plugins.slots import open_items
from homeoa.registry.models import OwnershipPeriod, Person, Unit
from homeoa.vendors.models import Vendor

TODAY = dt.date(2026, 10, 14)


@pytest.fixture
def two(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    out = []
    for name in ("Cedar Row", "Elm Court"):
        a = Association.objects.create(name=name, short_name=name[:2], timezone="UTC")
        Unit.all_associations.create(association=a, number="A", allocated_interest=Decimal("1"))
        out.append(a)
    return out


def unit(a):
    return Unit.all_associations.get(association=a, number="A")


def check(module_id, two, make_open, make_finished):
    """No items at first; one in-flight record is listed; a finished one and another
    association's record are not."""
    mine, other = two
    assert open_items(module_id, mine) == []
    make_finished(mine)
    make_open(other)
    assert open_items(module_id, mine) == []
    make_open(mine)
    items = open_items(module_id, mine)
    assert len(items) == 1
    assert str(items[0].description)


def test_open_violations_are_open_items(two):
    def make(a, status):
        cat = ViolationCategory.all_associations.get_or_create(association=a, name="Noise")[0]
        Violation.all_associations.create(
            association=a,
            unit=unit(a),
            category=cat,
            observed_on=TODAY,
            description="Loud music",
            status=status,
        )

    def finished(a):
        for s in ("cured", "closed", "dismissed"):
            make(a, s)

    check("violations", two, lambda a: make(a, "open"), finished)


def test_undecided_design_approval_requests_are_open_items(two):
    def make(a, status):
        form = ArcForm.all_associations.get_or_create(association=a, name="Standard")[0]
        person = Person.all_associations.create(association=a, full_name="Owner")
        ArcRequest.all_associations.create(
            association=a,
            unit=unit(a),
            submitted_by=person,
            form=form,
            title="Blue door",
            status=status,
            submitted_on=TODAY,
        )

    def finished(a):
        for s in ("approved", "approved_with_conditions", "denied", "withdrawn"):
            make(a, s)

    check("arc", two, lambda a: make(a, "under_review"), finished)


def test_maintenance_requests_not_closed_are_open_items(two):
    def make(a, status):
        MaintenanceRequest.all_associations.create(association=a, title="Leak", status=status)

    def finished(a):
        make(a, "done")
        make(a, "declined")

    check("owner_requests", two, lambda a: make(a, "in_progress"), finished)


def test_open_work_orders_are_open_items(two):
    def make(a, status):
        vendor = Vendor.all_associations.get_or_create(association=a, name="Plumber", slug="p")[0]
        WorkOrder.all_associations.create(
            association=a, vendor=vendor, scope="Fix leak", status=status
        )

    def finished(a):
        make(a, "completed")
        make(a, "cancelled")

    check("work_orders", two, lambda a: make(a, "scheduled"), finished)


def test_scheduled_unsent_announcements_are_open_items(two):
    def make(a, publish_on, **kw):
        Announcement.all_associations.create(
            association=a, title="Pool opens", publish_on=publish_on, **kw
        )

    def finished(a):
        make(a, TODAY)  # live, nothing to send
        make(a, TODAY, also_notify=True, notified_on=TODAY)  # sent

    check("announcements", two, lambda a: make(a, TODAY + dt.timedelta(days=3)), finished)


def test_open_polls_are_open_items(two):
    def make(a, opens_on, closes_on, **kw):
        Poll.all_associations.create(
            association=a,
            question="Paint the fence?",
            choices=["Yes", "No"],
            opens_on=opens_on,
            closes_on=closes_on,
            **kw,
        )

    def finished(a):
        make(a, dt.date(2026, 9, 1), dt.date(2026, 9, 30))
        make(a, dt.date(2026, 10, 1), dt.date(2026, 10, 30), closed_early_on=TODAY)

    check("polls", two, lambda a: make(a, dt.date(2026, 10, 1), dt.date(2026, 10, 30)), finished)


def test_active_autopay_and_untaken_debits_are_open_items(two):
    def enrollment(a, **kw):
        person = Person.all_associations.create(association=a, full_name="Owner")
        period = OwnershipPeriod.all_associations.get_or_create(
            association=a, unit=unit(a), start=dt.date(2020, 1, 1)
        )[0]
        return AutopayEnrollment.all_associations.create(
            association=a,
            unit=unit(a),
            ownership_period=period,
            person=person,
            rule="balance",
            mandate_ref="pm_test",
            starts_on=dt.date(2026, 1, 1),
            **kw,
        )

    def charge(a, e, status, month):
        AutopayCharge.all_associations.create(
            association=a, enrollment=e, unit=unit(a), period=month, amount="300.00", status=status
        )

    def finished(a):
        e = enrollment(a, cancelled_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))
        charge(a, e, AutopayStatus.FAILED, dt.date(2026, 8, 1))
        charge(a, e, AutopayStatus.SKIPPED, dt.date(2026, 9, 1))

    check("online_payments", two, enrollment, finished)

    mine = two[0]
    e = AutopayEnrollment.all_associations.filter(association=mine).first()
    charge(mine, e, AutopayStatus.SCHEDULED, dt.date(2026, 10, 1))
    AutopayEnrollment.all_associations.filter(association=mine, cancelled_at=None).update(
        cancelled_at=dt.datetime(2026, 10, 2, tzinfo=dt.UTC)
    )
    items = open_items("online_payments", mine)
    assert [str(i.description) for i in items] == ["Autopay debit not yet taken for unit A"]


def test_a_processing_autopay_debit_is_open_only_inside_the_return_window(two):
    mine = two[0]
    person = Person.all_associations.create(association=mine, full_name="Owner")
    period = OwnershipPeriod.all_associations.create(
        association=mine, unit=unit(mine), start=dt.date(2020, 1, 1)
    )
    e = AutopayEnrollment.all_associations.create(
        association=mine,
        unit=unit(mine),
        ownership_period=period,
        person=person,
        rule="balance",
        mandate_ref="pm_test",
        starts_on=dt.date(2026, 1, 1),
        cancelled_at=dt.datetime(2026, 10, 13, tzinfo=dt.UTC),
    )

    def debit(debit_on, month):
        AutopayCharge.all_associations.create(
            association=mine,
            enrollment=e,
            unit=unit(mine),
            period=month,
            amount="300.00",
            status=AutopayStatus.PENDING,
            debit_on=debit_on,
        )

    debit(dt.date(2026, 9, 23), dt.date(2026, 9, 1))  # 15 banking days before Wed 14 Oct
    assert open_items("online_payments", mine) == []
    debit(dt.date(2026, 10, 9), dt.date(2026, 10, 1))  # 3 banking days before
    assert len(open_items("online_payments", mine)) == 1


def test_a_poll_scheduled_to_open_later_is_an_open_item(two):
    Poll.all_associations.create(
        association=two[0],
        question="Paint the fence?",
        choices=["Yes", "No"],
        opens_on=dt.date(2026, 11, 1),
        closes_on=dt.date(2026, 11, 15),
    )
    assert len(open_items("polls", two[0])) == 1


@pytest.mark.parametrize("module_id", ["committees", "calendar", "directory", "board_packet"])
def test_modules_without_open_items_report_none(two, module_id):
    assert open_items(module_id, two[0]) == []
