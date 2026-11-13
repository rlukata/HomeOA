import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction

from homeoa.access.testing import grant
from homeoa.ap import approvals, services
from homeoa.ap.models import Bill, BillLine
from homeoa.audit.models import AuditEvent
from homeoa.banking.models import BankAccount
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger import services as ledger
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

START = dt.date(2020, 1, 1)
D = Decimal


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture(autouse=True)
def auto_approve(request):
    """These tests exercise posting, not approvals (see test_approvals.py): a
    0-approval rule makes submit approve at once."""
    if "assoc" not in request.fixturenames and "vendor" not in request.fixturenames:
        return
    request.getfixturevalue("assoc")
    from homeoa.ap.approvals.models import ApprovalRule

    ApprovalRule.objects.create(name="Auto", required_approvals=0)


@pytest.fixture
def vendor(assoc):
    return vendor_services.save_vendor(Vendor(name="City of Tukwila Utilities"))


def _user(email, assoc, **kw):
    user = get_user_model().objects.create_user(email=email)
    grant(user, association=assoc, start=START, **kw)
    return user


@pytest.fixture
def treasurer(assoc):
    return _user("t@example.org", assoc, title="treasurer")


@pytest.fixture
def director(assoc):
    return _user("d@example.org", assoc)


def _bill(vendor, number="INV-1", lines=(("5000", "operating", "612.34"),), **kw):
    bill = Bill.objects.create(
        vendor=vendor,
        invoice_number=number,
        bill_date=kw.pop("bill_date", dt.date(2026, 9, 30)),
        due_date=kw.pop("due_date", dt.date(2026, 10, 22)),
        description="Water & sewer",
        **kw,
    )
    for account, fund, amount in lines:
        BillLine.objects.create(
            bill=bill,
            account=Account.objects.get(code=account),
            fund=Fund.objects.get(code=fund),
            amount=D(amount),
        )
    return bill


def _bal(code, fund=None):
    f = Fund.objects.get(code=fund) if fund else None
    return ledger.account_balance(Account.objects.get(code=code), fund=f)


# --- posting -------------------------------------------------------------------------


def test_submit_posts_accrual_balanced_per_fund(vendor, treasurer):
    bill = _bill(
        vendor,
        lines=[
            ("5000", "operating", "500.00"),
            ("5200", "operating", "112.34"),
            ("5200", "reserve", "2000.00"),
        ],
    )
    bill = services.submit(bill, treasurer)
    assert bill.status == Bill.Status.APPROVED  # 0-approval rule
    assert bill.entry.date == dt.date(2026, 9, 30)
    assert _bal("2000", "operating") == D("612.34")
    assert _bal("2000", "reserve") == D("2000.00")
    assert _bal("5200", "reserve") == D("2000.00")
    assert _bal("5000") == D("500.00")
    assert ledger.trial_balance().difference == 0
    assert AuditEvent.objects.filter(action="ap.bill_submitted").exists()


def test_approval_hook_pending(vendor, treasurer, monkeypatch):
    calls = []
    monkeypatch.setattr(approvals, "required", lambda b: calls.append(b) or approvals.PENDING)
    bill = services.submit(_bill(vendor), treasurer)
    assert calls and bill.status == Bill.Status.PENDING and bill.approved_at is None


def test_submit_only_drafts_and_needs_lines(vendor, treasurer):
    empty = _bill(vendor, number="E", lines=[])
    with pytest.raises(services.BillError):
        services.submit(empty, treasurer)
    bill = services.submit(_bill(vendor), treasurer)
    with pytest.raises(services.BillError):
        services.submit(bill, treasurer)
    assert JournalEntry.objects.count() == 1


def test_non_expense_line_rejected(vendor, treasurer):
    bill = _bill(vendor, lines=[("1000", "operating", "10.00")])
    with pytest.raises(services.BillError):
        services.submit(bill, treasurer)


def test_void_reverses(vendor, treasurer):
    bill = services.submit(_bill(vendor), treasurer)
    bill = services.void(bill, treasurer, "Duplicate", date=dt.date(2026, 10, 2))
    assert bill.status == Bill.Status.VOID
    assert bill.void_entry.reversal_of_id == bill.entry_id
    assert _bal("2000") == 0 and _bal("5000") == 0
    with pytest.raises(services.BillError):
        services.void(bill, treasurer, "again")


def test_void_draft_posts_nothing(vendor, treasurer):
    bill = services.void(_bill(vendor), treasurer, "typo")
    assert bill.status == Bill.Status.VOID and not JournalEntry.objects.exists()


def test_locked_period_blocked(assoc, vendor, treasurer):
    ledger.set_locked_through(assoc, dt.date(2026, 9, 30))
    bill = _bill(vendor)
    with pytest.raises(services.BillError, match="locked"):
        services.submit(bill, treasurer)
    bill.refresh_from_db()
    assert bill.status == Bill.Status.DRAFT and not JournalEntry.objects.exists()


def test_duplicate_invoice_blocked_but_void_frees_number(vendor, treasurer):
    bill = _bill(vendor, number="A-1")
    with pytest.raises(IntegrityError), transaction.atomic():
        _bill(vendor, number="A-1", lines=[])
    services.void(bill, treasurer, "re-enter")
    assert _bill(vendor, number="A-1", lines=[]).pk


def test_mark_paid_clears_ap(assoc, vendor, treasurer):
    bank = BankAccount.objects.create(
        name="Operating",
        gl_account=Account.objects.get(code="1000"),
        fund=Fund.objects.get(code="operating"),
    )
    bill = services.submit(_bill(vendor), treasurer)
    op = Fund.objects.get(code="operating")
    bill = services.mark_paid(
        bill, treasurer, paid_on=dt.date(2026, 10, 3), bank_accounts={op: bank}
    )
    assert bill.status == Bill.Status.PAID and bill.paid_on == dt.date(2026, 10, 3)
    assert _bal("2000") == 0 and _bal("1000") == D("-612.34")
    assert AuditEvent.objects.filter(action="ap.bill_marked_paid").exists()
    with pytest.raises(services.BillError):
        services.void(bill, treasurer, "no")


def test_mark_paid_wrong_fund_rejected(vendor, treasurer):
    reserve_bank = BankAccount.objects.create(
        name="Reserve",
        gl_account=Account.objects.get(code="1010"),
        fund=Fund.objects.get(code="reserve"),
    )
    bill = services.submit(_bill(vendor), treasurer)
    op = Fund.objects.get(code="operating")
    with pytest.raises(services.BillError):
        services.mark_paid(
            bill, treasurer, paid_on=dt.date(2026, 10, 3), bank_accounts={op: reserve_bank}
        )


def test_bill_entry_needs_bill_access(vendor, director):
    """Any board member (or the Maintenance committee) enters and submits bills; the
    approval rules decide payment. Someone without a role cannot."""
    outsider = get_user_model().objects.create_user(email="x@example.org")
    bill = _bill(vendor)
    with pytest.raises(PermissionDenied):
        services.submit(bill, outsider)
    assert services.submit(bill, director).status != Bill.Status.DRAFT


# --- views ---------------------------------------------------------------------------


def _post_data(vendor, **kw):
    op = Fund.objects.get(code="operating")
    acct = Account.objects.get(code="5000")
    data = {
        "vendor": vendor.pk,
        "invoice_number": "INV-9",
        "bill_date": "2026-09-30",
        "due_date": "2026-10-22",
        "description": "Water",
        "lines-TOTAL_FORMS": "3",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        "lines-0-account": acct.pk,
        "lines-0-fund": op.pk,
        "lines-0-amount": "612.34",
        "lines-0-memo": "",
    }
    data.update(kw)
    return data


def test_add_and_submit_via_view(client, vendor, treasurer):
    client.force_login(treasurer)
    assert client.get("/money/bills/add/").status_code == 200
    pdf = SimpleUploadedFile("inv.pdf", b"%PDF-1.4 x", content_type="application/pdf")
    resp = client.post("/money/bills/add/", _post_data(vendor, submit="1", attachment=pdf))
    assert resp.status_code == 302
    bill = Bill.objects.get()
    assert bill.status == Bill.Status.APPROVED and bill.total == D("612.34")
    att = bill.attachments.get()
    ev = AuditEvent.objects.get(action="ap.bill_attachment_added")
    assert ev.message == "inv.pdf" and ev.actor == treasurer
    body = client.get(f"/money/bills/{bill.pk}/").content.decode()
    assert "attach-preview" in body and "612.34" in body
    file_resp = client.get(f"/money/bills/{bill.pk}/files/{att.pk}/")
    assert file_resp.status_code == 200 and file_resp.content == b"%PDF-1.4 x"
    # Submitted bills cannot be edited.
    assert client.get(f"/money/bills/{bill.pk}/edit/").status_code == 302
    # Duplicate invoice number blocked in the form.
    resp = client.post("/money/bills/add/", _post_data(vendor))
    assert resp.status_code == 400 and "already has a bill" in resp.content.decode()


def test_form_requires_a_line(client, vendor, treasurer):
    client.force_login(treasurer)
    data = _post_data(vendor)
    data["lines-0-amount"] = ""
    data["lines-0-account"] = ""
    data["lines-0-fund"] = ""
    assert client.post("/money/bills/add/", data).status_code == 400
    assert not Bill.objects.exists()


def test_list_filter_totals_overdue(client, assoc, vendor, director, treasurer):
    services.submit(_bill(vendor, number="OLD", due_date=dt.date(2000, 1, 1)), treasurer)
    _bill(vendor, number="DRAFT")
    client.force_login(director)
    body = client.get("/money/bills/").content.decode()
    assert "OLD" in body and "DRAFT" not in body and 'data-testid="bill-overdue"' in body
    assert 'data-testid="bill-total"' in body and "612.34" in body
    body = client.get("/money/bills/?status=draft", HTTP_HX_REQUEST="true").content.decode()
    assert body.startswith("<tbody") and "DRAFT" in body and "OLD" not in body
    assert "Add bill" not in client.get("/money/bills/").content.decode()


def test_permissions(client, vendor, director):
    bill = _bill(vendor)
    client.force_login(director)
    assert client.get(f"/money/bills/{bill.pk}/").status_code == 200
    assert client.get("/money/bills/add/").status_code == 200
    resident = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(resident)
    assert client.get("/money/bills/").status_code == 403


def test_void_and_mark_paid_views(client, vendor, treasurer):
    bank = BankAccount.objects.create(
        name="Operating",
        gl_account=Account.objects.get(code="1000"),
        fund=Fund.objects.get(code="operating"),
    )
    client.force_login(treasurer)
    bill = services.submit(_bill(vendor), treasurer)
    body = client.get(f"/money/bills/{bill.pk}/").content.decode()
    assert "bill-mark-paid" in body
    resp = client.post(
        f"/money/bills/{bill.pk}/paid/",
        {"paid_on": "2026-10-03", f"bank_{bank.fund_id}": bank.pk},
    )
    assert resp.status_code == 302
    bill.refresh_from_db()
    assert bill.status == Bill.Status.PAID
    other = services.submit(_bill(vendor, number="B"), treasurer)
    client.post(f"/money/bills/{other.pk}/void/", {"reason": "Duplicate"})
    other.refresh_from_db()
    assert other.status == Bill.Status.VOID and other.void_reason == "Duplicate"


def test_bills_subtab_wired(client, treasurer):
    client.force_login(treasurer)
    body = client.get("/money/bills/").content.decode()
    assert 'href="/money/bills/"' in body


def test_association_scope(vendor):
    _bill(vendor)
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert not Bill.objects.exists()
