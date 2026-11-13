# ruff: noqa: F811  (pytest fixtures imported from test_approvals)
import datetime as dt

from django.contrib.auth import get_user_model

from homeoa.access.testing import open_residents
from homeoa.ap import todos
from homeoa.ap.approvals.models import ApprovalRule
from homeoa.ap.models import Bill
from homeoa.ap.tests.test_approvals import (  # noqa: F401  (fixtures)
    _submitted,
    assoc,
    director,
    president,
    tiers,
    treasurer,
    vendor,
)
from homeoa.dashboard import todos as home_todos


def _ids(assoc):
    return {i["id"] for i in todos.todo_items(assoc)}


def test_todo_per_eligible_approver_appears_and_disappears(
    assoc, tiers, vendor, treasurer, president, director
):
    bill = _submitted(vendor, "1240.00", treasurer)
    ids = _ids(assoc)
    assert ids == {f"ap-approve-{bill.pk}-{president.pk}", f"ap-approve-{bill.pk}-{director.pk}"}
    item = todos.todo_items(assoc)[0]
    assert "0 of 2 approvals" in item["sub"] and "1,240.00" in item["title"]
    assert any(i.source == "bills" for i in home_todos.collect(assoc))
    from homeoa.ap.approvals import services

    services.approve(bill, president)
    assert _ids(assoc) == {f"ap-approve-{bill.pk}-{director.pk}"}
    services.approve(bill, director)
    assert _ids(assoc) == set()


def test_detail_progress_and_htmx_approve(client, assoc, tiers, vendor, treasurer, president):
    bill = _submitted(vendor, "1240.00", treasurer)
    client.force_login(president)
    body = client.get(f"/money/bills/{bill.pk}/").content.decode()
    assert "0 of 2 approvals" in body and 'data-testid="bill-approve"' in body
    resp = client.post(f"/money/bills/{bill.pk}/approve/", HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert body.lstrip().startswith("<section") and "1 of 2 approvals" in body
    assert 'data-testid="bill-approve"' not in body


def test_fully_approved_shows_pay_at_bank(client, assoc, vendor, treasurer, president):
    bill = _submitted(vendor, "50.00", treasurer)
    client.force_login(president)
    assert client.post(f"/money/bills/{bill.pk}/approve/").status_code == 302
    bill.refresh_from_db()
    assert bill.status == Bill.Status.APPROVED
    assert "Approved · pay at bank" in client.get(f"/money/bills/{bill.pk}/").content.decode()


def test_non_eligible_sees_no_button(client, assoc, vendor, treasurer):
    bill = _submitted(vendor, "50.00", treasurer)
    client.force_login(treasurer)  # submitter
    body = client.get(f"/money/bills/{bill.pk}/").content.decode()
    assert 'data-testid="bill-approve"' not in body
    assert "0 of 1 approvals" in body
    resident = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(resident)
    assert client.post(f"/money/bills/{bill.pk}/approve/").status_code == 403


def test_reject_requires_reason(client, assoc, vendor, treasurer, president):
    bill = _submitted(vendor, "50.00", treasurer)
    client.force_login(president)
    client.post(f"/money/bills/{bill.pk}/reject/", {"reason": ""})
    bill.refresh_from_db()
    assert bill.status == Bill.Status.PENDING
    client.post(f"/money/bills/{bill.pk}/reject/", {"reason": "Wrong amount"})
    bill.refresh_from_db()
    assert bill.status == Bill.Status.DRAFT
    assert "Wrong amount" in client.get(f"/money/bills/{bill.pk}/").content.decode()


def test_waiting_list_on_bills_page(client, assoc, vendor, treasurer, president):
    bill = _submitted(vendor, "50.00", treasurer)
    client.force_login(president)
    body = client.get("/money/bills/").content.decode()
    assert 'data-testid="bills-waiting"' in body and f"waiting-row-{bill.pk}" in body


def test_rules_page_lists_and_edits(client, assoc, tiers, director):
    client.force_login(director)
    body = client.get("/money/bills/approval-rules/").content.decode()
    assert "Under $1,000" in body and "$1,000 and up" in body
    rule = ApprovalRule.objects.get(priority=20)
    data = {
        "name": "Big",
        "priority": 20,
        "active": "on",
        "min_amount": "1000.00",
        "max_amount": "",
        "category": "",
        "vendor": "",
        "required_approvals": 3,
    }
    resp = client.post(f"/money/bills/approval-rules/{rule.pk}/", data)
    assert resp.status_code == 302
    rule.refresh_from_db()
    assert rule.name == "Big" and rule.required_approvals == 3
    resp = client.post("/money/bills/approval-rules/add/", {**data, "name": "New", "priority": 5})
    assert resp.status_code == 302 and ApprovalRule.objects.filter(name="New").exists()


def test_rules_page_needs_settings_manage(client, assoc):
    resident = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(resident)
    assert client.get("/money/bills/approval-rules/").status_code == 403


def test_residents_never_see_approval_todos(client, db, settings):
    """With a pending bill: owners and tenants are redirected from Home and get 403 from
    the to-do list; a board member sees the "Approve bill" to-do."""
    from homeoa.ap import services as bill_services
    from homeoa.ap.models import BillLine
    from homeoa.core import demo
    from homeoa.core.context import association_context
    from homeoa.ledger.models import Account, Fund
    from homeoa.vendors import services as vendor_services
    from homeoa.vendors.models import Vendor

    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    users = get_user_model().objects
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        treasurer = users.get(email="treasurer@cedarrow.example")
        v = vendor_services.save_vendor(Vendor(name="Rainier Roofing", category="Roofing"))
        bill = Bill.objects.create(
            vendor=v,
            invoice_number="RR-1",
            bill_date=dt.date(2026, 10, 1),
            created_by=treasurer,
        )
        BillLine.objects.create(
            bill=bill,
            account=Account.objects.get(code="5000"),
            fund=Fund.objects.get(code="operating"),
            amount="300.00",
        )
        bill_services.submit(bill, treasurer)
        assert Bill.objects.get(pk=bill.pk).status == Bill.Status.PENDING

    for email in ("ownerc@cedarrow.example", "chris.doyle@cedarrow.example"):
        client.force_login(users.get(email=email))
        home = client.get("/")
        assert home.status_code == 302, email
        todo = client.get("/todos/")
        assert todo.status_code == 403, email
        for resp in (home, todo):
            assert "Approve bill" not in resp.content.decode(), email

    client.force_login(users.get(email="secretary@cedarrow.example"))
    resp = client.get("/", follow=True)
    assert resp.status_code == 200
    assert "Approve bill" in resp.content.decode()


def test_approve_todo_shows_only_to_that_approver(
    client, assoc, tiers, vendor, treasurer, president, director
):
    """Collect(association, user) keeps another approver's items off your Home."""
    bill = _submitted(vendor, "1240.00", treasurer)
    mine = f"ap-approve-{bill.pk}-{president.pk}"
    theirs = f"ap-approve-{bill.pk}-{director.pk}"
    ids = {i.id for i in home_todos.collect(assoc, president)}
    assert mine in ids and theirs not in ids
    assert not any(i.id.startswith("ap-approve-") for i in home_todos.collect(assoc, treasurer))
    assert {mine, theirs} <= {i.id for i in home_todos.collect(assoc)}

    client.force_login(president)
    body = client.get("/").content.decode()
    assert f'id="todo-{mine}"' in body and f'id="todo-{theirs}"' not in body
