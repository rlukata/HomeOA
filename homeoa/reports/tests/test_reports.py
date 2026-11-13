import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant
from homeoa.ap import services as ap
from homeoa.ap.approvals.models import ApprovalRule
from homeoa.ap.models import Bill, BillLine
from homeoa.banking.models import BankAccount
from homeoa.billing.models import Charge, ChargeKind
from homeoa.billing.services import adjustments as adj
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.ledger.services import Line, post_entry
from homeoa.payments import services as pay
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership
from homeoa.reports import queries
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

D = Decimal
AS_OF = dt.date(2026, 10, 31)
CASH_CODES = {"1000", "1010", "1020", "1060"}


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        for n in "ABC":
            u = Unit.objects.create(number=n, allocated_interest=D("33.33"))
            p = Person.objects.create(full_name=f"Owner {n}")
            set_initial_ownership(u, [p], dt.date(2020, 1, 1))
        # Reports tests exercise posting, not approvals: a 0-approval rule makes
        # ap.submit approve at once, so mark_paid's approved-only guard is met.
        ApprovalRule.objects.create(name="Auto", required_approvals=0)
        yield a


@pytest.fixture
def treasurer(assoc):
    user = get_user_model().objects.create_user(email="t@example.org")
    grant(user, association=assoc, start=dt.date(2020, 1, 1), title="treasurer")
    return user


@pytest.fixture
def today(monkeypatch):
    """Pin the association clock used to choose live vs rebuilt open balances."""

    def set_today(day):
        monkeypatch.setattr(queries, "association_today", lambda _a: day)

    set_today(AS_OF)
    return set_today


def unit(n):
    return Unit.objects.get(number=n)


def charge(n, amount, date, due):
    return adj.manual_charge(
        unit(n), D(amount), "dues", f"Dues {date:%b}", kind=ChargeKind.ASSESSMENT,
        account="4000", date=date, due_date=due,
    )  # fmt: skip


def receipt(n, amount, date):
    return pay.record_receipt(unit=unit(n), amount=D(amount), received_date=date, method="check")


def bill(treasurer, name, amount, bill_date, due=None, account="5000"):
    vendor = vendor_services.save_vendor(Vendor(name=name))
    b = Bill.objects.create(
        vendor=vendor, invoice_number=f"{name}-1", bill_date=bill_date, due_date=due
    )
    BillLine.objects.create(
        bill=b,
        account=Account.objects.get(code=account),
        fund=Fund.objects.get(code="operating"),
        amount=D(amount),
    )
    return ap.submit(b, treasurer)


def pay_bill(treasurer, b, paid_on):
    bank, _ = BankAccount.objects.get_or_create(
        name="Operating",
        defaults={
            "gl_account": Account.objects.get(code="1000"),
            "fund": Fund.objects.get(code="operating"),
        },
    )
    return ap.mark_paid(b, treasurer, paid_on=paid_on, bank_accounts={bank.fund: bank})


def net(tb, code):
    return sum((r.debit - r.credit for r in tb.rows if r.account.code == code), D("0"))


def codes(tb):
    return {r.account.code for r in tb.rows}


@pytest.fixture
def books(assoc, treasurer):
    # A: July paid in full, September part paid then a $50 credit memo, October not due.
    charge("A", "300.00", dt.date(2026, 7, 1), dt.date(2026, 7, 1))
    sep_a = charge("A", "300.00", dt.date(2026, 9, 1), dt.date(2026, 9, 15))
    # B: Aug and Sep billed, paid in October except $80, which is written off.
    charge("B", "80.00", dt.date(2026, 8, 1), dt.date(2026, 8, 1))
    charge("B", "250.00", dt.date(2026, 9, 1), dt.date(2026, 9, 1))
    receipt("B", "250.00", dt.date(2026, 10, 3))  # oldest first: $80 Aug + $170 Sep
    receipt("A", "400.00", dt.date(2026, 10, 5))  # $300 July + $100 September
    charge("A", "300.00", dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    receipt("C", "100.00", dt.date(2026, 10, 10))  # prepayment, held in 2100
    adj.credit_memo(unit("A"), D("50.00"), "goodwill", date=dt.date(2026, 10, 20), charge=sep_a)
    adj.write_off(
        unit("B"), D("80.00"), "uncollectible", date=dt.date(2026, 10, 25),
        charge=Charge.objects.get(unit=unit("B"), open_balance__gt=0),
    )  # fmt: skip
    # Bills: December bill paid in January; one open and current; one open and late.
    dec = bill(treasurer, "Puget Water", "120.00", dt.date(2025, 12, 15), dt.date(2026, 1, 14))
    pay_bill(treasurer, dec, dt.date(2026, 1, 10))
    bill(treasurer, "Evergreen Landscaping", "500.00", dt.date(2026, 10, 10), dt.date(2026, 11, 9))
    bill(treasurer, "Cascade Electric", "80.00", dt.date(2026, 7, 15), dt.date(2026, 8, 14))
    return assoc


def test_trial_balance_balances_both_bases(books):
    for basis in queries.BASES:
        for as_of in (None, dt.date(2025, 12, 31), dt.date(2026, 9, 30), AS_OF):
            tb = queries.trial_balance(as_of=as_of, basis=basis, association=books)
            assert tb.difference == 0, (basis, as_of)


def test_cash_tb_excludes_open_ar_ap_and_prepaid(books):
    accrual = queries.trial_balance(as_of=AS_OF, association=books)
    cash = queries.trial_balance(as_of=AS_OF, basis=queries.CASH, association=books)
    assert {"1100", "2000", "2100"} <= codes(accrual)
    assert not {"1100", "2000", "2100", "5800"} & codes(cash)
    assert net(cash, "1000") == net(accrual, "1000") == D("630.00")
    # Received: 250 (B) + 400 (A) + 100 (C prepayment); paid: 120.
    assert net(cash, "4000") == D("-750.00")
    assert net(cash, "5000") == D("120.00")


def test_december_bill_paid_in_january(books):
    acct = Account.objects.get(code="5000")
    dec = dict(start=dt.date(2025, 12, 1), end=dt.date(2025, 12, 31), account=acct)
    jan = dict(start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 31), account=acct)
    assert queries.gl_detail(**dec, association=books)[0].closing == D("120.00")
    assert queries.gl_detail(**dec, basis=queries.CASH, association=books) == []
    (g,) = queries.gl_detail(**jan, basis=queries.CASH, association=books)
    assert g.opening == 0
    assert [(r.line.entry.date, r.line.debit) for r in g.rows] == [
        (dt.date(2026, 1, 10), D("120.00"))
    ]
    cash_dec = queries.trial_balance(
        as_of=dt.date(2025, 12, 31), basis=queries.CASH, association=books
    )
    assert cash_dec.rows == []


def test_dues_billed_september_paid_october(books):
    sep = queries.trial_balance(as_of=dt.date(2026, 9, 30), basis=queries.CASH, association=books)
    assert net(sep, "4000") == 0
    accrual_sep = queries.trial_balance(as_of=dt.date(2026, 9, 30), association=books)
    assert net(accrual_sep, "4000") == D("-930.00")
    oct_ = queries.period_activity(books, start=dt.date(2026, 10, 1), end=AS_OF)
    assert oct_["4000"] == D("-750.00")


@pytest.mark.parametrize(
    "start,end",
    [
        (dt.date(2025, 1, 1), dt.date(2026, 12, 31)),
        (dt.date(2025, 12, 1), dt.date(2025, 12, 31)),
        (dt.date(2026, 1, 1), dt.date(2026, 1, 31)),
        (dt.date(2026, 10, 1), dt.date(2026, 10, 31)),
        (dt.date(2026, 10, 4), dt.date(2026, 10, 20)),
    ],
)
def test_cash_income_statement_equals_net_bank_movement(books, start, end):
    """The cash-basis income statement is the bank statement, split by category."""
    act = queries.period_activity(books, start=start, end=end, basis=queries.CASH)
    accounts = {a.code: a for a in Account.objects.all()}
    bank = sum((v for c, v in act.items() if c in CASH_CODES), D("0"))
    pl = {c: v for c, v in act.items() if accounts[c].type in ("income", "expense")}
    assert set(act) - CASH_CODES == set(pl)  # nothing but bank and P&L categories
    assert -sum(pl.values(), D("0")) == bank
    assert "5800" not in pl  # write-off excluded, not income plus expense


def test_cash_period_categories(books):
    act = queries.period_activity(
        books, start=dt.date(2025, 1, 1), end=dt.date(2026, 12, 31), basis=queries.CASH
    )
    assert act == {"1000": D("630.00"), "4000": D("-750.00"), "5000": D("120.00")}


def test_receipt_void_reverses_cash_income(books):
    r = receipt("C", "40.00", dt.date(2026, 10, 12))
    pay.void_receipt(r, reason="bounced", date=dt.date(2026, 10, 14))
    act = queries.period_activity(
        books, start=dt.date(2026, 10, 11), end=dt.date(2026, 10, 31), basis=queries.CASH
    )
    assert act.get("4000", D("0")) == act.get("1000", D("0")) == 0


def test_ar_aging_live_by_due_date(books, today):
    aging = queries.ar_aging(as_of=AS_OF, association=books)
    rows = {r.label: r for r in aging.rows}
    a = rows[str(unit("A"))]
    assert a.buckets["current"] == D("300.00")  # October, due Nov 1
    assert a.buckets["d31"] == D("150.00")  # September: 300 - 100 - 50, due Sep 15
    assert str(unit("B")) not in rows
    assert rows[str(unit("C"))].credit == D("100.00")
    assert aging.total == D("450.00")


def test_ar_aging_past_date_rebuilt_from_allocations(books, today):
    today(dt.date(2026, 12, 31))
    rebuilt = queries.ar_aging(as_of=AS_OF, association=books)
    today(AS_OF)
    live = queries.ar_aging(as_of=AS_OF, association=books)
    assert [(r.label, r.cells) for r in rebuilt.rows] == [(r.label, r.cells) for r in live.rows]
    today(dt.date(2026, 12, 31))
    sep = queries.ar_aging(as_of=dt.date(2026, 9, 30), association=books)
    rows = {r.label: r for r in sep.rows}
    a, b = rows[str(unit("A"))], rows[str(unit("B"))]
    assert a.buckets["d90"] == D("300.00")  # July, 91 days past due
    assert a.buckets["d1"] == D("300.00")  # September, 15 days
    assert b.buckets["d31"] == D("80.00")  # August, 60 days
    assert b.buckets["d1"] == D("250.00")  # September, 29 days


def test_ar_aging_charge_less_receivable_by_posting_date(assoc, today):
    post_entry(
        [
            Line(account="1100", fund="operating", debit=D("90.00"), unit_id=unit("C").pk),
            Line(account="3000", fund="operating", credit=D("90.00")),
        ],
        date=dt.date(2026, 6, 1),
        memo="Imported balance",
    )
    (row,) = queries.ar_aging(as_of=AS_OF, association=assoc).rows
    assert row.buckets["d90"] == D("90.00")


def test_ap_aging_by_due_date(books):
    aging = queries.ap_aging(as_of=AS_OF, association=books)
    rows = {r.label: r for r in aging.rows}
    assert rows["Evergreen Landscaping"].buckets["current"] == D("500.00")
    assert rows["Cascade Electric"].buckets["d61"] == D("80.00")  # 78 days past Aug 14
    assert "Puget Water" not in rows
    assert aging.total == D("580.00")
    old = queries.ap_aging(as_of=dt.date(2025, 12, 31), association=books)
    assert [(r.label, r.buckets["current"]) for r in old.rows] == [("Puget Water", D("120.00"))]


def test_voided_bill_drops_out(books, treasurer):
    b = Bill.objects.get(vendor__name="Cascade Electric")
    ap.void(b, treasurer, "duplicate", date=AS_OF)
    aging = queries.ap_aging(as_of=AS_OF, association=books)
    assert aging.total == D("500.00")
    cash = queries.trial_balance(as_of=AS_OF, basis=queries.CASH, association=books)
    assert cash.difference == 0 and "2000" not in codes(cash)


def test_gl_cash_closing_matches_cash_tb(books):
    cash = queries.trial_balance(as_of=AS_OF, basis=queries.CASH, association=books)
    for code in ("1000", "4000", "5000"):
        (g,) = queries.gl_detail(
            start=dt.date(2026, 9, 1),
            end=AS_OF,
            account=Account.objects.get(code=code),
            basis=queries.CASH,
            association=books,
        )
        assert g.closing == net(cash, code), code


def test_views(books, client, treasurer):
    client.force_login(treasurer)
    for url in (
        "/money/reports/",
        "/money/reports/trial-balance/?basis=cash&as_of=2026-10-31",
        "/money/reports/gl/?account=4000&start=2026-09-01&basis=cash",
        "/money/reports/gl/?account=1100&start=2026-10-01",
        "/money/reports/ar-aging/?as_of=2026-10-31",
        "/money/reports/ap-aging/?as_of=2026-10-31&fund=operating",
    ):
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert "style=" not in resp.content.decode()
    html = client.get("/money/reports/trial-balance/").content.decode()
    assert 'data-testid="tb-difference">$0.00<' in html and "Assessments receivable" in html
    html = client.get("/money/reports/trial-balance/?basis=cash").content.decode()
    assert 'data-testid="tb-difference">$0.00<' in html and "Cash basis" in html
    html = client.get("/money/reports/ap-aging/?as_of=2026-10-31").content.decode()
    assert 'data-testid="aging-total">$580.00<' in html
    assert "Current" in html and "1-30 days" in html


def test_views_need_finance_read(assoc, client):
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    for url in ("/money/reports/", "/money/reports/trial-balance/", "/money/reports/gl/"):
        assert client.get(url).status_code == 403, url
    assert client.get("/money/reports/ar-aging/").status_code == 403
