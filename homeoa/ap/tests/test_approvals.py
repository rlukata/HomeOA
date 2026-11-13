import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction

from homeoa.access.testing import grant
from homeoa.ap import approvals, services
from homeoa.ap.approvals import notifications, rules
from homeoa.ap.approvals import services as approval_services
from homeoa.ap.approvals.forms import ApprovalRuleForm
from homeoa.ap.approvals.models import Approval, ApprovalRule
from homeoa.ap.models import Bill, BillLine
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger import services as ledger
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry
from homeoa.notify import services as notify_services
from homeoa.notify import types as ntypes
from homeoa.notify.models import Notification
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


@pytest.fixture
def vendor(assoc):
    return vendor_services.save_vendor(Vendor(name="Evergreen Landscaping", category="Landscaping"))


def _user(email, assoc, role="board", **kw):
    user = get_user_model().objects.create_user(email=email)
    grant(user, role=role, association=assoc, start=START, **kw)
    return user


@pytest.fixture
def treasurer(assoc):
    return _user("t@example.org", assoc, title="treasurer")


@pytest.fixture
def president(assoc):
    return _user("p@example.org", assoc, title="president")


@pytest.fixture
def director(assoc):
    return _user("d@example.org", assoc)


@pytest.fixture
def tiers(assoc):
    ApprovalRule.objects.create(
        name="Under $1,000", priority=10, max_amount=D("1000.00"), required_approvals=1
    )
    ApprovalRule.objects.create(
        name="$1,000 and up", priority=20, min_amount=D("1000.00"), required_approvals=2
    )


def _bill(vendor, amount, user, number="INV-1"):
    bill = Bill.objects.create(
        vendor=vendor,
        invoice_number=number,
        bill_date=dt.date(2026, 9, 30),
        description="Fall cleanup",
        created_by=user,
    )
    BillLine.objects.create(
        bill=bill,
        account=Account.objects.get(code="5000"),
        fund=Fund.objects.get(code="operating"),
        amount=D(amount),
    )
    return bill


def _submitted(vendor, amount, user, number="INV-1"):
    return services.submit(_bill(vendor, amount, user, number), user)


def _ap():
    return ledger.account_balance(Account.objects.get(code="2000"))


# --- rule matching ---------------------------------------------------------------------


def test_default_is_one_board_approval_without_submitter(vendor, treasurer):
    bill = _bill(vendor, "50.00", treasurer)
    req = rules.requirement_for(bill)
    assert req.rule is None and req.required == 1 and not req.allow_submitter
    assert approvals.required(bill) == approvals.PENDING


@pytest.mark.parametrize(
    "amount,count",
    [("485.00", 1), ("1240.00", 2), ("999.99", 1), ("1000.00", 2), ("0.01", 1)],
)
def test_thresholds(tiers, vendor, treasurer, amount, count):
    assert rules.requirement_for(_bill(vendor, amount, treasurer)).required == count


def test_priority_category_vendor_and_inactive(assoc, vendor, treasurer):
    other = vendor_services.save_vendor(Vendor(name="Puget Plumbing", category="Plumbing"))
    ApprovalRule.objects.create(name="Any", priority=50, required_approvals=1)
    ApprovalRule.objects.create(
        name="Landscaping", priority=20, category="landscaping", required_approvals=2
    )
    ApprovalRule.objects.create(name="Plumber", priority=10, vendor=other, required_approvals=3)
    ApprovalRule.objects.create(name="Off", priority=1, active=False, required_approvals=0)
    assert rules.requirement_for(_bill(vendor, "10.00", treasurer)).rule.name == "Landscaping"
    assert rules.requirement_for(_bill(other, "10.00", treasurer, "P1")).required == 3


def test_zero_rule_auto_approves(assoc, vendor, treasurer):
    ApprovalRule.objects.create(name="Small", max_amount=D("100.00"), required_approvals=0)
    assert _submitted(vendor, "99.99", treasurer).status == Bill.Status.APPROVED
    assert _submitted(vendor, "100.00", treasurer, "B").status == Bill.Status.PENDING


def test_rule_form_validates_range(assoc):
    form = ApprovalRuleForm(
        data={
            "name": "Bad",
            "priority": 1,
            "min_amount": "500.00",
            "max_amount": "500.00",
            "required_approvals": 1,
        },
        association=assoc,
    )
    assert not form.is_valid() and "max_amount" in form.errors
    form = ApprovalRuleForm(
        data={
            "name": "Big",
            "priority": 1,
            "active": "on",
            "min_amount": "1000.00",
            "required_approvals": 2,
            "eligible_roles": ["board"],
        },
        association=assoc,
    )
    assert form.is_valid(), form.errors
    rule = form.save()
    assert rule.association == assoc and rule.eligible_roles == ["board"]
    assert rule.min_amount == D("1000.00")


# --- approve ---------------------------------------------------------------------------


def test_485_needs_one_approval(tiers, vendor, treasurer, president):
    bill = _submitted(vendor, "485.00", treasurer)
    assert bill.status == Bill.Status.PENDING
    assert approval_services.progress(bill) == (0, 1)
    bill = approvals.approve(bill, president)
    assert bill.status == Bill.Status.APPROVED and bill.approved_at
    assert AuditEvent.objects.filter(action="ap.bill_approved").exists()


def test_1240_needs_two_and_same_person_twice_blocked(
    tiers, vendor, treasurer, president, director
):
    bill = _submitted(vendor, "1240.00", treasurer)
    bill = approvals.approve(bill, president)
    assert bill.status == Bill.Status.PENDING
    assert approval_services.progress(bill) == (1, 2)
    assert not approval_services.can_approve(bill, president)
    with pytest.raises(approval_services.ApprovalError):
        approvals.approve(bill, president)
    assert approval_services.can_approve(bill, director)
    bill = approvals.approve(bill, director)
    assert bill.status == Bill.Status.APPROVED
    assert Approval.objects.filter(bill=bill).count() == 2


def test_submitter_cannot_approve_unless_rule_allows(assoc, vendor, treasurer):
    bill = _submitted(vendor, "50.00", treasurer)
    assert not approval_services.can_approve(bill, treasurer)
    with pytest.raises(approval_services.ApprovalError):
        approvals.approve(bill, treasurer)
    ApprovalRule.objects.create(name="Self ok", allow_submitter=True)
    assert approvals.approve(bill, treasurer).status == Bill.Status.APPROVED


def test_non_board_and_role_limits(assoc, vendor, treasurer, director):
    owner = get_user_model().objects.create_user(email="o@example.org")
    bill = _submitted(vendor, "50.00", treasurer)
    with pytest.raises(PermissionDenied):
        approvals.approve(bill, owner)
    with pytest.raises(PermissionDenied):
        approvals.reject(bill, owner, "no")
    ApprovalRule.objects.create(name="CPA only", eligible_roles=["cpa"])
    with pytest.raises(PermissionDenied):
        approvals.approve(bill, director)


def test_only_pending_bills(assoc, vendor, treasurer, president):
    draft = _bill(vendor, "50.00", treasurer)
    with pytest.raises(approval_services.ApprovalError):
        approvals.approve(draft, president)
    with pytest.raises(approval_services.ApprovalError):
        approvals.reject(draft, president, "x")


# --- reject ----------------------------------------------------------------------------


def test_reject_reverses_accrual_and_resubmit_starts_new_round(
    tiers, vendor, treasurer, president, director
):
    bill = _submitted(vendor, "1240.00", treasurer)
    first_entry = bill.entry
    assert _ap() == D("1240.00")
    bill = approvals.approve(bill, president)

    with pytest.raises(approval_services.ApprovalError):
        approvals.reject(bill, director, "  ")
    bill = approvals.reject(bill, director, "Wrong amount")
    assert bill.status == Bill.Status.DRAFT and bill.entry is None
    assert bill.submitted_at is None
    assert _ap() == 0
    assert JournalEntry.objects.filter(reversal_of=first_entry).exists()
    assert ledger.trial_balance().difference == 0
    assert AuditEvent.objects.filter(action="ap.bill_rejected", message="Wrong amount").exists()
    assert Approval.objects.get(decision="rejected").reason == "Wrong amount"

    bill = services.submit(bill, treasurer)
    assert bill.entry_id != first_entry.pk and _ap() == D("1240.00")
    # The earlier approval belonged to the first submission and no longer counts.
    assert approval_services.progress(bill) == (0, 2)
    bill = approvals.approve(bill, president)
    bill = approvals.approve(bill, director)
    assert bill.status == Bill.Status.APPROVED


def test_void_after_reject_posts_nothing_more(assoc, vendor, treasurer, president):
    bill = approvals.reject(_submitted(vendor, "50.00", treasurer), president, "Duplicate")
    entries = JournalEntry.objects.count()
    bill = services.void(bill, treasurer, "dup")
    assert bill.status == Bill.Status.VOID and JournalEntry.objects.count() == entries


# --- approval emails ---


def _notes(type_key):
    return list(Notification.all_associations.filter(type_key=type_key).order_by("pk"))


def test_types_registered(assoc):
    assert ntypes.get(notifications.AWAITING_TYPE)
    assert ntypes.get(notifications.DECIDED_TYPE)


def test_submit_notifies_approvers_after_commit(
    vendor, treasurer, president, director, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        bill = _submitted(vendor, "50.00", treasurer)
    assert not _notes(notifications.AWAITING_TYPE)  # nothing before commit
    for cb in callbacks:
        cb()
    notes = _notes(notifications.AWAITING_TYPE)
    users = {n.recipient.user_id for n in notes}
    assert users == {president.pk, director.pk}  # not the submitter
    assert str(bill) in notes[0].subject
    assert "/ap/" in notes[0].body or str(bill.pk) in notes[0].body


def test_auto_approved_bill_sends_nothing(
    assoc, vendor, treasurer, president, django_capture_on_commit_callbacks
):
    from decimal import Decimal

    from homeoa.ap.approvals.models import ApprovalRule

    ApprovalRule.objects.create(name="Small", max_amount=Decimal("100.00"), required_approvals=0)
    with django_capture_on_commit_callbacks(execute=True):
        assert _submitted(vendor, "10.00", treasurer).status == Bill.Status.APPROVED
    assert not _notes(notifications.AWAITING_TYPE)


def test_approve_notifies_submitter_once_fully_approved(
    tiers, vendor, treasurer, president, director, django_capture_on_commit_callbacks
):
    bill = _submitted(vendor, "1240.00", treasurer)
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.approve(bill, president)
    assert not _notes(notifications.DECIDED_TYPE)  # 1 of 2
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.approve(bill, director)
    notes = _notes(notifications.DECIDED_TYPE)
    assert len(notes) == 1 and notes[0].recipient.user_id == treasurer.pk
    assert "approved" in notes[0].subject


def test_reject_notifies_submitter_with_reason(
    vendor, treasurer, president, django_capture_on_commit_callbacks
):
    bill = _submitted(vendor, "50.00", treasurer)
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.reject(bill, president, "Duplicate invoice")
    (note,) = _notes(notifications.DECIDED_TYPE)
    assert note.recipient.user_id == treasurer.pk
    assert "rejected" in note.subject and "Duplicate invoice" in note.body


def test_resubmit_notifies_again(vendor, treasurer, president, django_capture_on_commit_callbacks):
    bill = _submitted(vendor, "50.00", treasurer)
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.reject(bill, president, "Wrong amount")
    with django_capture_on_commit_callbacks(execute=True):
        services.submit(Bill.objects.get(pk=bill.pk), treasurer)
    assert len(_notes(notifications.AWAITING_TYPE)) == 1  # first submit's callback discarded
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.reject(Bill.objects.get(pk=bill.pk), president, "Still wrong")
    assert len(_notes(notifications.DECIDED_TYPE)) == 2


def test_rollback_sends_nothing(vendor, treasurer, president, django_capture_on_commit_callbacks):
    bill = _submitted(vendor, "50.00", treasurer)
    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        approval_services.approve(bill, president)
        raise RuntimeError
    assert not _notes(notifications.DECIDED_TYPE)


def test_failing_send_never_rolls_back(
    vendor, treasurer, president, monkeypatch, django_capture_on_commit_callbacks
):
    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(notify_services, "notify", boom)
    bill = _submitted(vendor, "50.00", treasurer)
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.approve(bill, president)
    assert Bill.objects.get(pk=bill.pk).status == Bill.Status.APPROVED
    assert AuditEvent.objects.filter(action="notify.send_failed").exists()


def test_failing_recipient_lookup_never_raises(
    vendor, treasurer, president, monkeypatch, django_capture_on_commit_callbacks
):
    def boom(*a, **k):
        raise RuntimeError("no person")

    monkeypatch.setattr(notifications, "person_for", boom)
    with django_capture_on_commit_callbacks(execute=True):
        bill = _submitted(vendor, "50.00", treasurer)
    assert Bill.objects.get(pk=bill.pk).status == Bill.Status.PENDING
    assert AuditEvent.objects.filter(action="notify.send_failed").exists()


def test_submitter_without_current_role_gets_no_decision_email(
    vendor, treasurer, president, django_capture_on_commit_callbacks
):
    from homeoa.access.models import RoleAssignment

    bill = _submitted(vendor, "50.00", treasurer)
    RoleAssignment.all_associations.filter(user=treasurer).update(end=START)
    with django_capture_on_commit_callbacks(execute=True):
        approval_services.reject(bill, president, "Duplicate invoice")
    assert Bill.objects.get(pk=bill.pk).status == Bill.Status.DRAFT
    assert not _notes(notifications.DECIDED_TYPE)


def test_reserve_fund_bills_need_the_rule_packs_minimum(assoc, vendor, treasurer, monkeypatch):
    """WA (RCW 64.34.372(2)): reserve-fund spending needs two officers or directors."""
    from homeoa import rules as rule_values

    real = rule_values.value

    def value(association, key, *a, **kw):
        return 2 if key == "reserves.spend_min_approvals" else real(association, key, *a, **kw)

    monkeypatch.setattr(rule_values, "value", value)
    bill = _bill(vendor, "50.00", treasurer)
    assert rules.requirement_for(bill).required == 1  # operating fund only
    BillLine.objects.create(
        bill=bill,
        account=Account.objects.get(code="5000"),
        fund=Fund.objects.filter(is_reserve=True).first(),
        amount=D("10.00"),
    )
    req = rules.requirement_for(bill)
    assert req.required == 2 and not req.allow_submitter
