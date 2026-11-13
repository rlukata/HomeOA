"""Maintenance requests and work orders (MNT-01, MNT-02)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import close_residents, grant, open_residents
from homeoa.ap.approvals.models import Approval
from homeoa.ap.models import Bill, BillLine
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.maintenance import services
from homeoa.maintenance.models import MaintenanceRequest, WorkOrder
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, Unit
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

User = get_user_model()
D = Decimal
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(
        name="Cedar Row",
        short_name="CR",
        timezone="America/Los_Angeles",
    )
    open_residents(a)
    load_chart_of_accounts(a)
    with association_context(a):
        for n in "ABCDEFGHI":
            Unit.objects.create(number=n, allocated_interest=D("11.11"))
        yield a


def _unit(n):
    return Unit.objects.get(number=n)


@pytest.fixture
def board(assoc):
    user = User.objects.create_user("board@example.org")
    grant(user, association=assoc, start=dt.date(2020, 1, 1))
    return user


def _owner(n, email):
    usr = User.objects.create_user(email)
    person = Person.objects.create(full_name=email, email=email, user=usr)
    reg.set_initial_ownership(_unit(n), [person], dt.date(2020, 1, 1))
    return usr, person


def _tenant(n, email):
    usr = User.objects.create_user(email)
    person = Person.objects.create(full_name=email, email=email, user=usr)
    Occupancy.objects.create(
        unit=_unit(n), person=person, kind=Occupancy.Kind.TENANT, start=dt.date(2024, 1, 1)
    )
    return usr, person


@pytest.fixture
def vendor(assoc):
    return vendor_services.save_vendor(Vendor(name="Evergreen Gutters"))


def _bill(vendor, number, amount):
    bill = Bill.objects.create(vendor=vendor, invoice_number=number, bill_date=dt.date(2026, 10, 1))
    BillLine.objects.create(
        bill=bill,
        account=Account.objects.get(code="5200"),
        fund=Fund.objects.get(code="operating"),
        amount=D(amount),
    )
    return bill


def _photo(name="leak.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


def test_board_enters_request_on_behalf_with_photo(client, assoc, board):
    _, person = _owner("G", "g@example.org")
    client.force_login(board)
    resp = client.post(
        "/requests/maintenance/add/",
        {
            "unit": _unit("G").pk,
            "title": "Gutter leak",
            "priority": "normal",
            "submitted_by": person.pk,
            "photos": [_photo()],
        },
    )
    assert resp.status_code == 302
    req = MaintenanceRequest.objects.get()
    assert req.submitted_by == person and req.entered_by == board
    assert req.photos.count() == 1
    assert req.photos.get().version.document.visibility == "board"
    page = client.get(f"/requests/maintenance/{req.pk}/")
    assert b"Gutter leak" in page.content
    photo = req.photos.get()
    assert client.get(f"/requests/maintenance/{req.pk}/photos/{photo.pk}/").content == PNG
    assert AuditEvent.objects.filter(object_type__icontains="maintenancerequest").exists()


def test_non_image_photo_rejected(client, assoc, board):
    client.force_login(board)
    resp = client.post(
        "/requests/maintenance/add/",
        {
            "title": "x",
            "priority": "normal",
            "photos": [SimpleUploadedFile("a.pdf", b"%PDF-1", content_type="application/pdf")],
        },
    )
    assert resp.status_code == 400
    assert not MaintenanceRequest.objects.exists()


def test_tenant_submits_and_sees_only_own(client, assoc):
    tenant, tperson = _tenant("I", "t@example.org")
    owner, operson = _owner("D", "d@example.org")
    other = services.submit_request(title="Owner's deck", unit=_unit("D"), submitted_by=operson)
    client.force_login(tenant)
    resp = client.post(
        "/me/maintenance/new/",
        {"unit": _unit("I").pk, "title": "Gutter leak", "priority": "normal", "photos": [_photo()]},
    )
    assert resp.status_code == 302, resp.content
    mine = MaintenanceRequest.objects.get(submitted_by=tperson)
    assert mine.unit == _unit("I") and mine.photos.count() == 1
    listing = client.get("/me/maintenance/")
    assert b"Gutter leak" in listing.content and b"Owner&#x27;s deck" not in listing.content
    assert client.get(f"/me/maintenance/{other.pk}/").status_code == 404
    assert client.get(f"/me/maintenance/{mine.pk}/").status_code == 200
    # Tenants cannot reach board pages.
    assert client.get("/requests/maintenance/").status_code == 403


def test_resident_cannot_pick_someone_elses_unit(client, assoc):
    tenant, _ = _tenant("I", "t@example.org")
    client.force_login(tenant)
    resp = client.post(
        "/me/maintenance/new/", {"unit": _unit("A").pk, "title": "x", "priority": "normal"}
    )
    assert resp.status_code == 200
    assert not MaintenanceRequest.objects.exists()


def test_resident_pages_closed_while_switch_off(client, assoc):
    close_residents(assoc)
    tenant, _ = _tenant("I", "t@example.org")
    client.force_login(tenant)
    assert client.get("/me/maintenance/new/").status_code == 403


def test_work_order_from_request_and_cost_from_bills(client, assoc, board, vendor):
    req = services.submit_request(title="Gutter leak", unit=_unit("I"))
    client.force_login(board)
    resp = client.post(
        f"/requests/maintenance/{req.pk}/work-order/",
        {"vendor": vendor.pk, "scope": "Fix gutter", "estimated_cost": "300.00"},
    )
    assert resp.status_code == 302
    wo = WorkOrder.objects.get()
    req.refresh_from_db()
    assert wo.request == req and req.status == "in_progress"
    b1, b2 = _bill(vendor, "E-1", "120.10"), _bill(vendor, "E-2", "79.95")
    for b in (b1, b2):
        assert (
            client.post(f"/requests/work-orders/{wo.pk}/bills/", {"bill": b.pk}).status_code == 302
        )
    assert wo.actual_cost == D("200.05") and isinstance(wo.actual_cost, Decimal)
    b2.status = Bill.Status.VOID
    b2.save()
    assert wo.actual_cost == D("120.10")
    detail = client.get(f"/money/bills/{b1.pk}/")
    assert f"/requests/work-orders/{wo.pk}/".encode() in detail.content
    assert AuditEvent.objects.filter(action="maintenance.bill_linked").count() == 2


def test_bill_link_limited_to_vendor_and_association(assoc, vendor):
    other_vendor = vendor_services.save_vendor(Vendor(name="Other Roofing"))
    wo = services.create_work_order(vendor=vendor, scope="x")
    foreign = _bill(other_vendor, "O-1", "10.00")
    assert foreign not in services.linkable_bills(wo)
    with pytest.raises(services.MaintenanceError):
        services.link_bill(wo, foreign)
    other = Association.objects.create(name="Other", short_name="O")
    load_chart_of_accounts(other)
    with association_context(other):
        v2 = vendor_services.save_vendor(Vendor(name="Evergreen Gutters"))
        far = _bill(v2, "X-1", "5.00")
    with pytest.raises(services.MaintenanceError):
        services.link_bill(wo, far)


def test_linking_leaves_bill_and_approvals_untouched(assoc, vendor):
    wo = services.create_work_order(vendor=vendor, scope="x")
    bill = _bill(vendor, "E-1", "10.00")
    before = (bill.status, bill.entry_id, Approval.objects.count())
    services.link_bill(wo, bill)
    bill.refresh_from_db()
    assert (bill.status, bill.entry_id, Approval.objects.count()) == before


def test_status_changes_history_and_audit(assoc, board, vendor):
    wo = services.create_work_order(vendor=vendor, scope="x", user=board)
    services.set_work_order_status(wo, "scheduled", user=board, scheduled_for=dt.date(2026, 10, 20))
    services.set_work_order_status(wo, "completed", user=board, note="done")
    assert [h.new_status for h in wo.history.all()] == ["draft", "scheduled", "completed"]
    assert AuditEvent.objects.filter(action="maintenance.work_order_status").count() == 2


def test_todos(assoc, vendor, monkeypatch):
    req = services.submit_request(title="Gutter leak", unit=_unit("I"))
    wo = services.create_work_order(vendor=vendor, scope="x", scheduled_for=dt.date(2026, 10, 1))
    services.set_work_order_status(wo, "scheduled")
    monkeypatch.setattr(services, "association_today", lambda a: dt.date(2026, 10, 9))
    ids = {i["id"] for i in services.todo_items(assoc)}
    assert ids == {f"maint-triage-{req.pk}", f"maint-wo-late-{wo.pk}"}
    services.set_work_order_status(wo, "completed")
    ids = {i["id"] for i in services.todo_items(assoc)}
    assert f"maint-wo-late-{wo.pk}" not in ids


def test_scoping(assoc):
    services.submit_request(title="A", unit=_unit("A"))
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert not MaintenanceRequest.objects.exists()


def test_print_page(client, assoc, board, vendor):
    wo = services.create_work_order(vendor=vendor, scope="Clean gutters")
    client.force_login(board)
    resp = client.get(f"/requests/work-orders/{wo.pk}/print/")
    assert resp.status_code == 200 and b"Clean gutters" in resp.content


def _treasurer(assoc):
    user = User.objects.create_user("treasurer@example.org")
    grant(user, association=assoc, start=dt.date(2020, 1, 1), title="treasurer")
    return user


def _bill_post(vendor, wo, amount="300.00"):
    return {
        "work_order": wo.pk,
        "vendor": vendor.pk,
        "invoice_number": "WO-1",
        "bill_date": "2026-10-01",
        "due_date": "2026-10-31",
        "description": "Gutter",
        "lines-TOTAL_FORMS": "3",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        "lines-0-account": Account.objects.get(code="5000").pk,
        "lines-0-fund": Fund.objects.get(code="operating").pk,
        "lines-0-amount": amount,
        "lines-0-memo": "",
    }


def test_create_bill_from_work_order_prefills_and_links(client, assoc, board, vendor):
    wo = services.create_work_order(vendor=vendor, scope="Fix gutter", estimated_cost=D("300.00"))
    client.force_login(board)
    page = client.get(f"/requests/work-orders/{wo.pk}/").content.decode()
    assert f"work_order={wo.pk}" in page
    client.force_login(_treasurer(assoc))
    form = client.get(f"/money/bills/add/?vendor={vendor.pk}&work_order={wo.pk}")
    assert form.status_code == 200
    html = form.content.decode()
    assert 'name="lines-0-amount"' in html and 'value="300.00"' in html
    assert f'name="work_order" value="{wo.pk}"' in html
    resp = client.post("/money/bills/add/", _bill_post(vendor, wo, "312.50"))
    assert resp.status_code == 302
    bill = Bill.objects.get(invoice_number="WO-1")
    assert list(wo.bills.all()) == [bill]
    assert wo.actual_cost == D("312.50")
    assert AuditEvent.objects.filter(action="maintenance.bill_linked").count() == 1


def test_create_bill_from_work_order_other_vendor_not_linked(client, assoc, board, vendor):
    other = vendor_services.save_vendor(Vendor(name="Other Roofing"))
    wo = services.create_work_order(vendor=vendor, scope="x")
    client.force_login(_treasurer(assoc))
    resp = client.post("/money/bills/add/", _bill_post(other, wo))
    assert resp.status_code == 302
    assert Bill.objects.filter(invoice_number="WO-1").exists()
    assert not wo.bills.exists()


# --- one-off spending limit ------------------------------------------------------


def _committee_member(assoc):
    user = get_user_model().objects.create_user(email="mc@example.org")
    grant(user, "maintenance_committee", association=assoc, start=dt.date(2020, 1, 1))
    return user


def test_one_off_jobs_under_the_limit_go_ahead(client, assoc, board, vendor):
    from homeoa.changes.models import ChangeRequest
    from homeoa.maintenance import oneoff

    oneoff.set_limit(assoc, Decimal("1000.00"))
    member = _committee_member(assoc)
    client.force_login(member)
    wo = services.create_work_order(vendor=vendor, scope="Wasp nest", estimated_cost=Decimal("180"))
    client.post(f"/requests/work-orders/{wo.pk}/status/", {"status": "sent"})
    wo.refresh_from_db()
    assert wo.status == "sent" and not ChangeRequest.objects.exists()


def test_jobs_over_the_limit_need_board_approval(client, assoc, board, vendor):
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest
    from homeoa.maintenance import oneoff

    oneoff.set_limit(assoc, Decimal("1000.00"))
    member = _committee_member(assoc)
    second = get_user_model().objects.create_user(email="b2@example.org")
    grant(second, association=assoc, start=dt.date(2020, 1, 1))
    client.force_login(member)
    wo = services.create_work_order(vendor=vendor, scope="Re-roof", estimated_cost=Decimal("8000"))
    resp = client.post(
        f"/requests/work-orders/{wo.pk}/status/", {"status": "scheduled"}, follow=True
    )
    assert "over the one-off limit" in resp.content.decode()
    wo.refresh_from_db()
    assert wo.status == "draft"
    change = ChangeRequest.objects.get()
    assert change.required_approvals == 2  # the committee is not the board
    # Asking again does not pile up requests.
    client.post(f"/requests/work-orders/{wo.pk}/status/", {"status": "scheduled"})
    assert ChangeRequest.objects.count() == 1
    changes.approve(change, board)
    changes.approve(change, second)
    wo.refresh_from_db()
    assert wo.status == "scheduled"


def test_job_without_estimate_needs_approval(assoc, board, vendor):
    from homeoa.maintenance import oneoff

    oneoff.set_limit(assoc, Decimal("1000.00"))
    wo = services.create_work_order(vendor=vendor, scope="Mystery leak")
    assert oneoff.needs_approval(wo, "sent")
    assert not oneoff.needs_approval(wo, "cancelled")


def test_raising_a_commissioned_estimate_over_the_limit_needs_approval(assoc, board, vendor):
    from homeoa.maintenance import oneoff

    oneoff.set_limit(assoc, Decimal("1000.00"))
    wo = services.create_work_order(
        vendor=vendor, scope="Pest control", estimated_cost=Decimal("300")
    )
    services.set_work_order_status(wo, "sent")
    wo.refresh_from_db()
    assert not oneoff.estimate_needs_approval(wo, Decimal("900"))
    assert oneoff.estimate_needs_approval(wo, Decimal("1500"))


def test_changing_the_limit_is_a_change_request(client, assoc, board):
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest
    from homeoa.maintenance import oneoff

    second = get_user_model().objects.create_user(email="b2@example.org")
    grant(second, association=assoc, start=dt.date(2020, 1, 1))
    client.force_login(board)
    client.post("/governance/changes/limits/", {"oneoff_limit": "1000", "reason": "Bylaws"})
    assert oneoff.limit(assoc) == Decimal("0")
    changes.approve(ChangeRequest.objects.get(), second)
    assoc.refresh_from_db()
    assert oneoff.limit(assoc) == Decimal("1000")
