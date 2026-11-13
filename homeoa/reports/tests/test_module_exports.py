"""Each optional feature's records download as CSV from Reports, even with the feature
switched off."""

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest

from homeoa.access.testing import grant
from homeoa.accounts.models import User
from homeoa.arc.models import ArcForm, ArcRequest
from homeoa.community.models import Announcement, Poll, PollAnswer
from homeoa.core.models import Association
from homeoa.enforcement.models import Violation, ViolationCategory
from homeoa.maintenance.models import MaintenanceRequest, WorkOrder
from homeoa.payments.models import AutopayCharge, AutopayEnrollment, AutopayStatus
from homeoa.plugins import modules
from homeoa.registry.models import OwnershipPeriod, Person, Unit
from homeoa.vendors.models import Vendor

TODAY = dt.date(2026, 10, 14)


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(name="Birch Lane", short_name="BL", timezone="UTC")
    Unit.all_associations.create(association=a, number="A", allocated_interest=Decimal("1"))
    return a


def _unit(a):
    return Unit.all_associations.get(association=a, number="A")


def make_violations(a):
    cat = ViolationCategory.all_associations.create(association=a, name="Noise")
    Violation.all_associations.create(
        association=a,
        unit=_unit(a),
        category=cat,
        observed_on=TODAY,
        description="Loud music",
        status="closed",
    )


def make_design_requests(a):
    form = ArcForm.all_associations.create(association=a, name="Standard")
    person = Person.all_associations.create(association=a, full_name="Owner")
    ArcRequest.all_associations.create(
        association=a,
        unit=_unit(a),
        submitted_by=person,
        form=form,
        title="Blue door",
        status="approved",
        submitted_on=TODAY,
        decided_on=TODAY,
    )


def make_maintenance_requests(a):
    MaintenanceRequest.all_associations.create(association=a, title="Leak", status="done")


def make_work_orders(a):
    vendor = Vendor.all_associations.create(association=a, name="Plumber", slug="p")
    WorkOrder.all_associations.create(
        association=a, vendor=vendor, scope="Fix leak", status="completed"
    )


def make_announcements(a):
    Announcement.all_associations.create(association=a, title="Pool opens", publish_on=TODAY)


def make_polls(a):
    p = Poll.all_associations.create(
        association=a,
        question="Paint the fence?",
        choices=["Yes", "No"],
        opens_on=dt.date(2026, 9, 1),
        closes_on=dt.date(2026, 9, 30),
    )
    person = Person.all_associations.create(association=a, full_name="Voter")
    PollAnswer.all_associations.create(
        association=a, poll=p, unit=_unit(a), person=person, choice=0
    )


def make_autopay(a):
    person = Person.all_associations.create(association=a, full_name="Owner")
    period = OwnershipPeriod.all_associations.create(
        association=a, unit=_unit(a), start=dt.date(2020, 1, 1)
    )
    e = AutopayEnrollment.all_associations.create(
        association=a,
        unit=_unit(a),
        ownership_period=period,
        person=person,
        rule="balance",
        mandate_ref="pm_test",
        starts_on=dt.date(2026, 1, 1),
        cancelled_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    AutopayCharge.all_associations.create(
        association=a,
        enrollment=e,
        unit=_unit(a),
        period=dt.date(2026, 8, 1),
        amount=Decimal("300.00"),
        status=AutopayStatus.FAILED,
    )


CASES = [
    ("violations", "violations", make_violations, 1, "Loud music"),
    ("design-requests", "arc", make_design_requests, 1, "Blue door"),
    ("maintenance-requests", "owner_requests", make_maintenance_requests, 1, "Leak"),
    ("work-orders", "work_orders", make_work_orders, 1, "Fix leak"),
    ("announcements", "announcements", make_announcements, 1, "Pool opens"),
    ("polls", "polls", make_polls, 1, "Yes: 1; No: 0"),
    ("autopay", "online_payments", make_autopay, 2, "300.00"),
]


def board_client(client, a):
    user = User.objects.create_user("board@example.org")
    grant(user, association=a)
    client.force_login(user)
    return client


@pytest.mark.parametrize(("slug", "module_id", "make", "rows", "text"), CASES)
def test_a_features_records_download_as_csv_while_the_feature_is_off(
    assoc, client, slug, module_id, make, rows, text
):
    make(assoc)
    modules.set_module(Association.objects.get(pk=assoc.pk), module_id, False, user=None)
    resp = board_client(client, assoc).get(f"/money/reports/exports/{slug}.csv")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")
    table = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    header, body = table[0], [r for r in table[1:] if any(r)]
    assert header and len(body) == rows
    assert any(text in cell for cell in body[-1])


def test_the_reports_page_lists_the_feature_exports(assoc, client):
    html = board_client(client, assoc).get("/money/reports/").content.decode()
    assert 'data-testid="export-violations"' in html
    assert 'data-testid="export-autopay"' in html


def test_someone_off_the_board_cannot_download_a_feature_export(assoc, client):
    make_violations(assoc)
    client.force_login(User.objects.create_user("owner@example.org"))
    assert client.get("/money/reports/exports/violations.csv").status_code == 403


def test_an_unknown_feature_export_is_not_found(assoc, client):
    assert board_client(client, assoc).get("/money/reports/exports/laundry.csv").status_code == 404
