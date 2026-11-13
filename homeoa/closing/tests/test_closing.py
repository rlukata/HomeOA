import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from homeoa.access.testing import grant
from homeoa.ap.models import Bill
from homeoa.audit.models import AuditEvent
from homeoa.banking.models import BankAccount, Reconciliation
from homeoa.closing import services
from homeoa.closing.models import Close
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import CLOSE_SOURCE, load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalLine
from homeoa.ledger.services import Line, PeriodLocked, get_locked_through, post_entry
from homeoa.reports import statements as st
from homeoa.reports.queries import ACCRUAL, CASH
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

D = Decimal


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Test Condo", short_name="TC", timezone="UTC")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def user(assoc):
    u = get_user_model().objects.create_user("treas@example.com", password="x")
    grant(u, association=assoc, title="treasurer")
    return u


def post(debit_code, credit_code, amount, date, fund="operating"):
    return post_entry(
        [
            Line(account=debit_code, fund=fund, debit=D(amount)),
            Line(account=credit_code, fund=fund, credit=D(amount)),
        ],
        date=date,
        memo="test",
    )


def reconcile(statement_date, fund="operating", gl="1000"):
    bank, _ = BankAccount.objects.get_or_create(
        name=f"Bank {fund}",
        defaults={"gl_account": Account.objects.get(code=gl), "fund": Fund.objects.get(code=fund)},
    )
    Reconciliation.objects.create(
        bank_account=bank,
        statement_date=statement_date,
        beginning_balance=D("0"),
        ending_balance=D("0"),
        completed_at=timezone.now(),
    )
    return bank


def year_activity():
    post("1000", "4000", "1000.00", dt.date(2026, 1, 15))
    post("5200", "1000", "300.00", dt.date(2026, 6, 10))
    post("1010", "4000", "500.00", dt.date(2026, 3, 1), fund="reserve")
    post("5200", "1010", "800.00", dt.date(2026, 4, 1), fund="reserve")


# ---------------------------------------------------------------- checklist


def test_checklist_items(assoc):
    start, end = services.month_bounds(2026, 1)
    checks = {c.key: c.ok for c in services.checklist(assoc, start, end)}
    assert checks == {"bank_recs": True, "draft_bills": True, "trial_balance": True}

    bank = reconcile(dt.date(2025, 12, 31))  # before the month: not enough
    assert not {c.key: c.ok for c in services.checklist(assoc, start, end)}["bank_recs"]
    Reconciliation.objects.create(
        bank_account=bank,
        statement_date=dt.date(2026, 1, 31),
        beginning_balance=D("0"),
        ending_balance=D("0"),
    )  # not completed
    assert not {c.key: c.ok for c in services.checklist(assoc, start, end)}["bank_recs"]
    reconcile(dt.date(2026, 1, 31))
    assert {c.key: c.ok for c in services.checklist(assoc, start, end)}["bank_recs"]

    vendor = vendor_services.save_vendor(Vendor(name="Plumber"))
    Bill.objects.create(vendor=vendor, invoice_number="1", bill_date=dt.date(2026, 1, 20))
    checks = {c.key: c.ok for c in services.checklist(assoc, start, end)}
    assert not checks["draft_bills"]
    # A draft dated after the month does not block it.
    assert {c.key: c.ok for c in services.checklist(assoc, *services.month_bounds(2025, 12))}[
        "draft_bills"
    ]


def test_close_month_refuses_failing_checklist(assoc, user):
    vendor = vendor_services.save_vendor(Vendor(name="Plumber"))
    Bill.objects.create(vendor=vendor, invoice_number="1", bill_date=dt.date(2026, 1, 20))
    with pytest.raises(services.CloseError):
        services.close_month(assoc, 2026, 1, user=user)
    assert get_locked_through(assoc) is None


# ---------------------------------------------------------------- month close + lock


def test_posting_into_closed_month_blocked(assoc, user):
    post("1000", "4000", "100.00", dt.date(2026, 1, 15))
    close = services.close_month(assoc, 2026, 1, user=user)
    assert close.period_end == dt.date(2026, 1, 31)
    assert get_locked_through(assoc) == dt.date(2026, 1, 31)
    with pytest.raises(PeriodLocked):
        post("1000", "4000", "1.00", dt.date(2026, 1, 31))
    post("1000", "4000", "1.00", dt.date(2026, 2, 1))  # next month is open
    assert AuditEvent.objects.filter(action="closing.close").count() == 1


def test_closes_go_forward_only(assoc, user):
    services.close_month(assoc, 2026, 3, user=user)
    with pytest.raises(services.CloseError):
        services.close_month(assoc, 2026, 2, user=user)
    with pytest.raises(services.CloseError):
        services.close_month(assoc, 2026, 3, user=user)


def test_reopen_needs_reason_and_is_audited(assoc, user):
    services.close_month(assoc, 2026, 1, user=user)
    feb = services.close_month(assoc, 2026, 2, user=user)
    mar = services.close_month(assoc, 2026, 3, user=user)
    with pytest.raises(services.CloseError):
        services.reopen(feb, reason="  ", user=user)
    reopened = services.reopen(feb, reason="Late utility bill", user=user)
    assert {c.pk for c in reopened} == {feb.pk, mar.pk}
    assert get_locked_through(assoc) == dt.date(2026, 1, 31)
    post("5200", "1000", "40.00", dt.date(2026, 2, 10))  # February is open again
    feb.refresh_from_db()
    assert feb.reopen_reason == "Late utility bill" and feb.reopened_by == user
    events = AuditEvent.objects.filter(action="closing.reopen")
    assert events.count() == 2
    assert all(e.message == "Late utility bill" for e in events)
    with pytest.raises(services.CloseError):
        services.reopen(feb, reason="again", user=user)
    # It can be closed again after the fix.
    services.close_month(assoc, 2026, 2, user=user)
    assert get_locked_through(assoc) == dt.date(2026, 2, 28)


# ---------------------------------------------------------------- year end


def _pl_by_fund(assoc, end):
    out = {}
    for f in Fund.objects.all():
        lines = JournalLine.objects.filter(
            fund=f, entry__date__lte=end, account__type__in=("income", "expense")
        )
        out[f.code] = sum((ln.debit - ln.credit for ln in lines), D("0"))
    return out


def test_year_end_zeroes_income_and_expense_per_fund(assoc, user):
    year_activity()
    services.close_month(assoc, 2026, 12, user=user)  # last month already locked
    close = services.close_year(assoc, 2026, user=user)
    assert close.entry is not None
    assert close.entry.date == dt.date(2026, 12, 31)
    assert close.entry.source_type == "ledger.close"
    assert _pl_by_fund(assoc, dt.date(2026, 12, 31)) == {
        "operating": D("0"),
        "reserve": D("0"),
        "insurance": D("0"),
    }
    fb = {
        ln.fund.code: ln.credit - ln.debit
        for ln in close.entry.lines.filter(account__type="equity").select_related("fund")
    }
    assert fb == {"operating": D("700.00"), "reserve": D("-300.00")}
    assert get_locked_through(assoc) == dt.date(2026, 12, 31)
    with pytest.raises(PeriodLocked):
        post("1000", "4000", "1.00", dt.date(2026, 7, 1))


@pytest.mark.parametrize("basis", (ACCRUAL, CASH))
def test_closed_year_statements_still_show_activity(assoc, user, basis):
    year_activity()
    jan1, dec31 = dt.date(2026, 1, 1), dt.date(2026, 12, 31)
    before = st.income_statement(assoc, start=jan1, end=dec31, basis=basis)
    services.close_year(assoc, 2026, user=user)
    after = st.income_statement(assoc, start=jan1, end=dec31, basis=basis)
    assert after.value("net_income", st.TOTAL) == before.value("net_income", st.TOTAL) == D("400")
    bs = st.balance_sheet(assoc, as_of=dec31, basis=ACCRUAL)
    assert bs.value("net_income", st.TOTAL) == D("0")
    assert bs.value("3000", "operating") == D("700.00")
    cf = st.cash_flow(assoc, start=jan1, end=dec31, basis=basis)
    assert cf.value("change", st.TOTAL) == cf.value("bank", st.TOTAL)


@pytest.mark.parametrize("basis", (ACCRUAL, CASH))
def test_statement_exports_exclude_closing_entry(assoc, user, basis):
    from homeoa.reports import tables
    from homeoa.reports.export import to_csv

    year_activity()
    jan1, dec31 = dt.date(2026, 1, 1), dt.date(2026, 12, 31)

    def csvs():
        out = []
        for fn, name in ((st.income_statement, "income"), (st.cash_flow, "cash-flow")):
            rep = fn(assoc, start=jan1, end=dec31, basis=basis)
            spec = tables.statement(
                rep, title=name, filename=name, period="2026", scope="All funds", basis=basis
            )
            out.append(to_csv(spec))
        return out

    before = csvs()
    close = services.close_year(assoc, 2026, user=user)
    assert close.entry.source_type == CLOSE_SOURCE
    assert csvs() == before


def test_reopen_year_reverses_closing_entry(assoc, user):
    year_activity()
    close = services.close_year(assoc, 2026, user=user)
    services.reopen(close, reason="Auditor adjustment", user=user)
    close.refresh_from_db()
    assert close.reversal_entry is not None
    assert close.reversal_entry.date == dt.date(2026, 12, 31)
    assert _pl_by_fund(assoc, dt.date(2026, 12, 31))["operating"] == D("-700.00")
    assert get_locked_through(assoc) is None
    post("5200", "1000", "100.00", dt.date(2026, 12, 15))
    again = services.close_year(assoc, 2026, user=user)
    assert again.pk != close.pk
    assert _pl_by_fund(assoc, dt.date(2026, 12, 31))["operating"] == D("0")


def test_year_close_with_no_activity_posts_nothing(assoc, user):
    close = services.close_year(assoc, 2025, user=user)
    assert close.entry is None
    assert get_locked_through(assoc) == dt.date(2025, 12, 31)


def test_fiscal_year_bounds_follow_start_month(assoc):
    assoc.fiscal_year_start_month = 7
    assert services.fiscal_year_bounds(assoc, 2026) == (dt.date(2026, 7, 1), dt.date(2027, 6, 30))


# ---------------------------------------------------------------- views


@pytest.fixture
def board_client(client, user):
    client.force_login(user)
    return client


def test_index_renders(board_client):
    r = board_client.get(reverse("closing:index") + "?month=2026-01&year=2025")
    assert r.status_code == 200
    assert b'data-testid="close-month"' in r.content
    assert b'data-testid="check-bank_recs"' in r.content


def test_close_and_reopen_through_views(board_client, assoc):
    r = board_client.post(reverse("closing:close_month"), {"month": "2026-01"})
    assert r.status_code == 302
    close = Close.objects.get()
    assert get_locked_through(assoc) == dt.date(2026, 1, 31)
    r = board_client.post(reverse("closing:reopen", args=[close.pk]), {"reason": ""})
    close.refresh_from_db()
    assert close.is_active
    board_client.post(reverse("closing:reopen", args=[close.pk]), {"reason": "Fix"})
    close.refresh_from_db()
    assert not close.is_active
    board_client.post(reverse("closing:close_year"), {"year": "2025"})
    assert Close.objects.filter(kind="year").exists()


def test_reopening_closed_books_needs_a_second_board_member(client, assoc, user):
    """Reopening is always a change request, even for the Treasurer."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    close = services.close_month(assoc, 2026, 1, user=user)
    member = get_user_model().objects.create_user("board@example.com", password="x")
    grant(member, association=assoc)  # board, no treasurer title
    client.force_login(user)
    client.post(reverse("closing:reopen", args=[close.pk]), {"reason": "Because"})
    close.refresh_from_db()
    assert close.is_active
    change = ChangeRequest.objects.get()
    assert change.summary.startswith("Reopen the closed books")
    changes.approve(change, member)
    close.refresh_from_db()
    assert not close.is_active


def test_closing_a_month_needs_a_post(board_client):
    r = board_client.get(reverse("closing:close_month"), {"month": "2026-01"})
    assert r.status_code == 405
    assert not Close.objects.exists()


def test_other_board_members_propose_a_close_from_the_same_page(client, assoc, user):
    """A board member who is not the Treasurer sees the close button; closing goes to the
    Treasurer for approval instead of locking the books."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    member = get_user_model().objects.create_user("board@example.com", password="x")
    grant(member, association=assoc)
    client.force_login(member)
    r = client.get(reverse("closing:index") + "?month=2026-01&year=2025")
    assert b'data-testid="close-month"' in r.content
    client.post(reverse("closing:close_month"), {"month": "2026-01"})
    assert not Close.objects.exists()
    change = ChangeRequest.objects.get()
    changes.approve(change, user)
    assert get_locked_through(assoc) == dt.date(2026, 1, 31)


def test_bank_rec_must_cover_period_end(assoc):
    start, end = services.month_bounds(2026, 1)
    bank = reconcile(dt.date(2026, 1, 20))  # inside the month, before its end
    assert not {c.key: c.ok for c in services.checklist(assoc, start, end)}["bank_recs"]
    Reconciliation.objects.create(
        bank_account=bank,
        statement_date=dt.date(2026, 2, 5),
        beginning_balance=D("0"),
        ending_balance=D("0"),
        completed_at=timezone.now(),
    )
    assert {c.key: c.ok for c in services.checklist(assoc, start, end)}["bank_recs"]


def test_second_association_isolated(client, assoc, user, monkeypatch):
    from homeoa.core.middleware import CurrentAssociationMiddleware

    close = services.close_month(assoc, 2026, 1, user=user)
    other = Association.objects.create(name="Other HOA", short_name="OH", timezone="UTC")
    load_chart_of_accounts(other)
    with association_context(other):
        BankAccount.objects.create(
            name="B bank",
            gl_account=Account.objects.get(code="1000"),
            fund=Fund.objects.get(code="operating"),
        )
        vendor = vendor_services.save_vendor(Vendor(name="B vendor"))
        Bill.objects.create(vendor=vendor, invoice_number="9", bill_date=dt.date(2026, 2, 3))
    # A's checklist ignores B's unreconciled bank account and draft bill.
    checks = {c.key: c.ok for c in services.checklist(assoc, *services.month_bounds(2026, 2))}
    assert checks["bank_recs"] and checks["draft_bills"]

    b_user = get_user_model().objects.create_user("b@example.com", password="x")
    grant(b_user, association=other, title="treasurer")
    monkeypatch.setattr(CurrentAssociationMiddleware, "resolve", lambda self, request: other)
    client.force_login(b_user)
    r = client.post(reverse("closing:reopen", args=[close.pk]), {"reason": "Because"})
    assert r.status_code == 404
    close.refresh_from_db()
    assert close.is_active
