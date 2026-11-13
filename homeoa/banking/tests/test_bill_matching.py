"""An outgoing bank transaction pays an approved bill (FIN-12, FIN-07)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from homeoa.access.testing import grant
from homeoa.ap import services as ap
from homeoa.ap.approvals.models import ApprovalRule
from homeoa.ap.models import Bill, BillLine
from homeoa.audit.models import AuditEvent
from homeoa.banking import matching, services
from homeoa.banking.models import BankAccount, BankTransaction, Match, MatchKind, TxnStatus
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry
from homeoa.ledger.services import account_balance
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

D = Decimal
PAID = dt.date(2026, 10, 6)
APPROVED_AT = timezone.make_aware(dt.datetime(2026, 10, 2, 9, 0))


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def treasurer(assoc):
    u = get_user_model().objects.create_user("t@example.org")
    grant(u, association=assoc, start=dt.date(2020, 1, 1), title="treasurer")
    return u


@pytest.fixture
def operating(assoc):
    return BankAccount.objects.create(
        name="Operating checking",
        gl_account=Account.objects.get(code="1000"),
        fund=Fund.objects.get(code="operating"),
    )


@pytest.fixture
def city(assoc):
    return vendor_services.save_vendor(Vendor(name="City of Tukwila Utilities"))


def _bill(vendor, user, amount="612.34", number="INV-1", fund="operating", approve=True):
    if approve:
        ApprovalRule.objects.get_or_create(name="Auto", defaults={"required_approvals": 0})
    bill = Bill.objects.create(vendor=vendor, invoice_number=number, bill_date=dt.date(2026, 9, 30))
    BillLine.objects.create(
        bill=bill,
        account=Account.objects.get(code="5000"),
        fund=Fund.objects.get(code=fund),
        amount=D(amount),
    )
    bill = ap.submit(bill, user)
    if bill.approved_at:
        Bill.objects.filter(pk=bill.pk).update(approved_at=APPROVED_AT)
        bill.refresh_from_db()
    return bill


def _debit(account, desc="CITY OF TUKWILA", amount="-612.34", date=PAID):
    services.import_transactions(
        account,
        [
            FeedTxn(
                external_id=f"{desc}-{amount}-{date}",
                posted=date,
                amount=D(amount),
                description=desc,
            )
        ],
        source="csv",
    )
    return BankTransaction.objects.get(external_id=f"{desc}-{amount}-{date}")


def _bal(code):
    return account_balance(Account.objects.get(code=code))


def test_city_of_tukwila_debit_suggests_the_bill(operating, city, treasurer):
    bill = _bill(city, treasurer)
    txn = _debit(operating)
    sugg = matching.suggestions(txn)
    assert sugg[0].kind == "bill" and sugg[0].extra["bill"] == bill
    assert sugg[0].reason == "exact amount and vendor name match"
    assert sugg[0].amount == D("612.34")


def test_vendor_default_account_suggested_when_no_bill_matches(operating, city, treasurer):
    """A debit naming a vendor suggests the vendor's default expense account, unless an
    approved bill for the amount is there to be paid."""
    Vendor.objects.filter(pk=city.pk).update(
        default_expense_account=Account.objects.get(code="5000")
    )
    txn = _debit(operating, desc="CITY OF TUKWILA UTILITIES 1005", amount="-612.34")
    (s,) = matching.suggestions(txn)
    assert s.kind == "categorize" and s.account.code == "5000"
    assert s.reason == "usual account for City of Tukwila Utilities"
    _bill(city, treasurer)
    assert [s.kind for s in matching.suggestions(txn)] == ["bill"]


def test_amount_only_scores_lower_and_wrong_amount_not_suggested(operating, city, treasurer):
    bill = _bill(city, treasurer)
    other = _debit(operating, desc="ONLINE PMT 88123")
    (s,) = [s for s in matching.suggestions(other) if s.kind == "bill"]
    assert s.extra["bill"] == bill and s.score < 95
    off = _debit(operating, amount="-612.35")
    assert not [s for s in matching.suggestions(off) if s.kind == "bill"]


def test_only_approved_bills_and_date_after_approval(operating, city, treasurer):
    _bill(city, treasurer, approve=False)  # pending: no rule, needs approval
    txn = _debit(operating)
    assert not [s for s in matching.suggestions(txn) if s.kind == "bill"]
    pending = Bill.objects.get()
    assert pending.status == Bill.Status.PENDING
    with pytest.raises(matching.MatchRefused, match="approved"):
        matching.confirm_bill(txn, pending, treasurer)
    assert Bill.objects.get().status == Bill.Status.PENDING

    early = _debit(operating, date=dt.date(2026, 10, 1))
    Bill.objects.filter(pk=pending.pk).update(status=Bill.Status.APPROVED, approved_at=APPROVED_AT)
    assert not [s for s in matching.suggestions(early) if s.kind == "bill"]
    assert [s for s in matching.suggestions(txn) if s.kind == "bill"]


def test_other_fund_bill_suggested(operating, city, treasurer):
    bill = _bill(city, treasurer, fund="reserve")
    (s,) = [s for s in matching.suggestions(_debit(operating)) if s.kind == "bill"]
    assert s.extra["bill"] == bill


def test_confirm_clears_ap_and_links(operating, city, treasurer):
    bill = _bill(city, treasurer)
    txn = _debit(operating)
    assert _bal("2000") == D("612.34")
    match = matching.confirm_bill(txn, bill, treasurer)
    bill.refresh_from_db()
    txn.refresh_from_db()
    assert bill.status == Bill.Status.PAID and bill.paid_on == PAID
    assert match.kind == MatchKind.BILL and match.bill == bill
    assert match.entry == bill.payment_entry
    assert bill.payment_entry.source_type == ap.SOURCE_PAYMENT
    assert txn.status == TxnStatus.MATCHED
    assert _bal("2000") == 0 and _bal("1000") == D("-612.34")
    assert AuditEvent.objects.filter(action="ap.bill_marked_paid").exists()
    assert AuditEvent.objects.filter(action="bank.bill").exists()


def test_no_double_match(operating, city, treasurer):
    bill = _bill(city, treasurer)
    txn = _debit(operating)
    twin = _debit(operating, desc="CITY OF TUKWILA DUP")
    matching.confirm_bill(txn, bill, treasurer)
    assert not [s for s in matching.suggestions(twin) if s.kind == "bill"]
    with pytest.raises(matching.MatchRefused):
        matching.confirm_bill(twin, bill, treasurer)
    with pytest.raises(matching.MatchRefused):
        matching.confirm_bill(txn, bill, treasurer)
    assert Match.objects.filter(bill=bill).count() == 1
    assert _bal("2000") == 0 and _bal("1000") == D("-612.34")


def test_unmatch_reverses_and_reopens_bill(operating, city, treasurer):
    bill = _bill(city, treasurer)
    txn = _debit(operating)
    matching.confirm_bill(txn, bill, treasurer)
    payment = Bill.objects.get().payment_entry
    matching.undo(txn, treasurer)
    bill.refresh_from_db()
    txn.refresh_from_db()
    assert bill.status == Bill.Status.APPROVED and bill.paid_on is None
    assert bill.payment_entry is None
    assert JournalEntry.objects.filter(reversal_of=payment).exists()
    assert txn.status == TxnStatus.UNMATCHED
    assert _bal("2000") == D("612.34") and _bal("1000") == 0
    assert AuditEvent.objects.filter(action="ap.bill_payment_reversed").exists()
    # It can be matched again afterwards.
    matching.confirm_bill(txn, bill, treasurer)
    assert Bill.objects.get().status == Bill.Status.PAID


def test_deposit_cannot_pay_a_bill(operating, city, treasurer):
    bill = _bill(city, treasurer)
    dep = _debit(operating, amount="612.34")
    with pytest.raises(matching.MatchRefused):
        matching.confirm_bill(dep, bill, treasurer)


# ------------------------------------------------------------------ views


def test_view_confirm_and_undo(client, operating, city, treasurer):
    client.force_login(treasurer)
    bill = _bill(city, treasurer)
    txn = _debit(operating)
    html = client.get("/money/bank/").content.decode()
    assert "Suggested: pays bill" in html
    html = client.get(f"/money/bank/txn/{txn.pk}/").content.decode()
    assert 'data-testid="bill-suggestion"' in html
    resp = client.post(f"/money/bank/txn/{txn.pk}/confirm/", {"bill": bill.pk}, follow=True)
    assert "is paid" in resp.content.decode()
    assert 'data-testid="matched-bill"' in resp.content.decode()
    assert Bill.objects.get().status == Bill.Status.PAID
    client.post(f"/money/bank/txn/{txn.pk}/undo/")
    assert Bill.objects.get().status == Bill.Status.APPROVED


def test_view_refuses_unapproved(client, operating, city, treasurer):
    client.force_login(treasurer)
    _bill(city, treasurer, approve=False)
    txn = _debit(operating)
    resp = client.post(
        f"/money/bank/txn/{txn.pk}/confirm/", {"bill": Bill.objects.get().pk}, follow=True
    )
    assert "Only an approved bill can be matched" in resp.content.decode()
    assert Bill.objects.get().status == Bill.Status.PENDING


def test_board_member_match_waits_for_approval(client, assoc, operating, city, treasurer):
    """A board member who is not the Treasurer proposes the match; the bill is paid only
    once another board member approves."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    member = get_user_model().objects.create_user("b@example.org")
    grant(member, association=assoc, start=dt.date(2020, 1, 1))
    bill = _bill(city, treasurer)
    txn = _debit(operating)
    client.force_login(member)
    resp = client.post(f"/money/bank/txn/{txn.pk}/confirm/", {"bill": bill.pk})
    assert resp.status_code == 302
    assert Bill.objects.get().status == Bill.Status.APPROVED
    change = ChangeRequest.objects.get()
    changes.approve(change, treasurer)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.APPLIED, change.error
    assert Bill.objects.get().status == Bill.Status.PAID


# ------------------------------------------------------------------ fix round


def test_pending_bill_gets_hint_only(operating, city, treasurer):
    _bill(city, treasurer, number="77", approve=False)
    txn = _debit(operating)
    (hint,) = [s for s in matching.suggestions(txn) if s.kind == "bill_pending"]
    assert hint.reason == "Matches bill #77 (awaiting approval). Approve it first."
    assert not [s for s in matching.suggestions(txn) if s.kind == "bill"]
    # No hint without the vendor name.
    other = _debit(operating, desc="ONLINE PMT 88123")
    assert not [s for s in matching.suggestions(other) if s.kind == "bill_pending"]


def test_pending_hint_view_has_no_confirm(client, operating, city, treasurer):
    client.force_login(treasurer)
    _bill(city, treasurer, number="77", approve=False)
    txn = _debit(operating)
    html = client.get(f"/money/bank/txn/{txn.pk}/").content.decode()
    assert 'data-testid="bill-pending-hint"' in html
    assert "Approve it first." in html
    assert 'name="bill"' not in html


def test_manual_payment_then_debit_links_without_double_count(operating, city, treasurer):
    bill = _bill(city, treasurer)
    ap.mark_paid(
        bill, treasurer, paid_on=dt.date(2026, 10, 4), bank_accounts={operating.fund: operating}
    )
    txn = _debit(operating)
    sugg = matching.suggestions(txn)
    assert sugg[0].kind == "bill_link" and sugg[0].extra["bill"] == bill
    assert not [s for s in sugg if s.kind == "bill"]
    before = JournalEntry.objects.count()
    match = matching.link_bill_payment(txn, bill, treasurer)
    assert JournalEntry.objects.count() == before
    assert match.posted is False and match.bill == bill
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED
    # AP, bank and expense each posted exactly once.
    assert _bal("2000") == 0
    assert _bal("1000") == D("-612.34")
    assert _bal("5000") == D("612.34")
    # Linked once only.
    twin = _debit(operating, desc="CITY OF TUKWILA DUP")
    assert not [s for s in matching.suggestions(twin) if s.kind == "bill_link"]
    with pytest.raises(matching.MatchRefused):
        matching.link_bill_payment(twin, bill, treasurer)
    # Undo unlinks only; the manual payment stands.
    matching.undo(txn, treasurer)
    assert Bill.objects.get().status == Bill.Status.PAID
    assert _bal("1000") == D("-612.34")


def _confirm_form_fields(html, testid):
    """Hidden inputs of the Confirm form in the suggestion row holding ``testid``."""
    import re

    row = html[html.index(f'data-testid="{testid}"') :]
    form = row[row.index("<form") : row.index("</form>")]
    return dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', form))


@pytest.mark.parametrize("manual", [False, True])
def test_rendered_confirm_form_posts_bill(client, operating, city, treasurer, manual):
    bill = _bill(city, treasurer)
    if manual:
        ap.mark_paid(bill, treasurer, paid_on=PAID, bank_accounts={operating.fund: operating})
    txn = _debit(operating)
    client.force_login(treasurer)
    html = client.get(f"/money/bank/txn/{txn.pk}/").content.decode()
    fields = _confirm_form_fields(html, "bill-link-suggestion" if manual else "bill-suggestion")
    assert fields.get("bill") == str(bill.pk)
    resp = client.post(f"/money/bank/txn/{txn.pk}/confirm/", fields)
    assert resp.status_code == 302
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED
    assert Match.objects.get(txn=txn).bill == bill
    assert _bal("1000") == D("-612.34") and _bal("2000") == 0


def test_categorize_warns_when_paid_bill_unlinked(client, operating, city, treasurer):
    from homeoa.banking.models import CategorizeRule

    CategorizeRule.objects.create(contains="TUKWILA", account=Account.objects.get(code="5000"))
    bill = _bill(city, treasurer)
    ap.mark_paid(bill, treasurer, paid_on=PAID, bank_accounts={operating.fund: operating})
    txn = _debit(operating)
    (cat,) = [s for s in matching.suggestions(txn) if s.kind == "categorize"]
    assert "already recorded" in cat.extra["warning"]
    client.force_login(treasurer)
    html = client.get(f"/money/bank/txn/{txn.pk}/categorize/").content.decode()
    assert 'data-testid="categorize-warning"' in html
    resp = client.post(f"/money/bank/txn/{txn.pk}/confirm/", {"bill": bill.pk}, follow=True)
    assert "Linked to the recorded payment" in resp.content.decode()
    assert _bal("1000") == D("-612.34")
