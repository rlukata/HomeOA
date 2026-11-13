import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.services import Line, post_entry
from homeoa.payments import services as pay
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership
from homeoa.reports import statements as st
from homeoa.reports.queries import ACCRUAL, BASES, CASH

D = Decimal
ZERO = D("0")
SEP30 = dt.date(2026, 9, 30)
NOTE = "Per-fund cash figures are net of interfund balances"


def assert_balanced(bs):
    for col in bs.columns:
        assets = bs.value("Total assets", col.key)
        other = bs.value("liab_fb", col.key)
        assert assets == other, (col.key, assets, other)


# ---------------------------------------------------------------- demo seed


@pytest.fixture
def demo(db):
    call_command("seed_demo", "--reset")
    a = Association.objects.get()
    with association_context(a):
        yield a


@pytest.mark.parametrize("basis", BASES)
def test_demo_balance_sheet_balances(demo, basis):
    for as_of in (dt.date(2026, 7, 31), dt.date(2026, 8, 31), SEP30):
        bs = st.balance_sheet(demo, as_of=as_of, basis=basis)
        assert_balanced(bs)
        assert bs.value("interfund", st.TOTAL) == ZERO  # nets to zero across funds
    bs = st.balance_sheet(demo, as_of=SEP30, basis=ACCRUAL)
    assert bs.value("1000", "operating") == D("14382.17")
    assert bs.value("interfund", "reserve") != ZERO  # shown per fund


@pytest.mark.parametrize("basis", BASES)
def test_net_income_equals_fund_balance_change(demo, basis):
    start, end = dt.date(2026, 8, 1), SEP30
    inc = st.income_statement(demo, start=start, end=end, basis=basis)
    before = st.balance_sheet(demo, as_of=start - dt.timedelta(days=1), basis=basis)
    after = st.balance_sheet(demo, as_of=end, basis=basis)
    for col in ("operating", "reserve", "insurance", st.TOTAL):
        change = after.value("Total fund balance", col) - before.value("Total fund balance", col)
        assert inc.value("net_income", col) == change, col


@pytest.mark.parametrize("basis", BASES)
def test_cash_flow_reconciles(demo, basis):
    cf = st.cash_flow(demo, start=dt.date(2026, 8, 1), end=SEP30, basis=basis)
    for col in ("operating", "reserve", "insurance", st.TOTAL):
        assert cf.value("change", col) == cf.value("bank", col), col
        bs = st.balance_sheet(demo, as_of=SEP30, basis=basis)
        cash = sum((bs.value(c, col) for c in ("1000", "1010", "1020", "1060")), ZERO)
        assert cf.value("end", col) == cash, col


def test_ytd_and_comparison_columns(demo):
    inc = st.income_statement(demo, start=dt.date(2026, 1, 1), end=SEP30, basis=ACCRUAL)
    keys = [c.key for c in inc.columns]
    assert keys == ["operating", "reserve", "insurance", st.TOTAL, st.COMPARE]
    assert inc.value("net_income", st.COMPARE) == ZERO  # nothing posted a year earlier
    one = st.income_statement(
        demo, start=dt.date(2026, 1, 1), end=SEP30, basis=ACCRUAL, fund_filter="reserve"
    )
    assert [c.key for c in one.columns] == ["reserve", st.COMPARE]
    assert one.value("net_income", "reserve") == inc.value("net_income", "reserve")
    total = st.income_statement(
        demo, start=dt.date(2026, 1, 1), end=SEP30, basis=ACCRUAL, fund_filter=st.ALL_TOTAL
    )
    assert not total.per_fund and [c.key for c in total.columns] == [st.TOTAL, st.COMPARE]
    assert st.fiscal_year_start(demo, SEP30) == dt.date(2026, 1, 1)


# ---------------------------------------------------------------- interfund + prepayment


@pytest.fixture
def books(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        u = Unit.objects.create(number="A", allocated_interest=D("100"))
        p = Person.objects.create(full_name="Owner A")
        set_initial_ownership(u, [p], dt.date(2020, 1, 1))
        # Dues collected into the operating bank; $100 of it is reserve income, owed
        # to the reserve fund until transferred (cash entry carrying 1900/2900).
        post_entry(
            [
                Line(account="1000", fund="operating", debit=D("400.00")),
                Line(account="4200", fund="operating", credit=D("300.00")),
                Line(account="2900", fund="operating", credit=D("100.00")),
                Line(account="1900", fund="reserve", debit=D("100.00")),
                Line(account="4200", fund="reserve", credit=D("100.00")),
            ],
            date=dt.date(2026, 10, 2),
            memo="Dues incl. reserve share",
        )
        pay.record_receipt(
            unit=u, amount=D("75.00"), received_date=dt.date(2026, 10, 5), method="check"
        )  # prepayment (no charge open)
        yield a


def test_cash_is_all_funds_ties_to_bank_per_fund_differs(books):
    start, end = dt.date(2026, 10, 1), dt.date(2026, 10, 31)
    inc = st.income_statement(books, start=start, end=end, basis=CASH, compare=False)
    cf = st.cash_flow(books, start=start, end=end, basis=CASH)
    bank_total = cf.value("bank", st.TOTAL)
    assert bank_total == D("475.00")
    assert inc.value("net_income", st.TOTAL) == bank_total
    for fund in ("operating", "reserve"):
        diff = inc.value("net_income", fund) - cf.value("bank", fund)
        assert abs(diff) == D("100.00"), fund  # off by the interfund amount
    bs = st.balance_sheet(books, as_of=end, basis=CASH)
    assert_balanced(bs)
    assert bs.value("interfund", "reserve") == D("100.00")
    assert bs.value("interfund", "operating") == D("-100.00")
    assert bs.value("interfund", st.TOTAL) == ZERO


def test_cash_prepayment_is_assessment_income(books):
    inc = st.income_statement(
        books, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), basis=CASH
    )
    assert inc.value("4000", "operating") == D("75.00")
    bs = st.balance_sheet(books, as_of=dt.date(2026, 10, 31), basis=CASH)
    assert bs.value("2100", st.TOTAL) == ZERO  # no prepaid-dues line on cash
    accrual = st.balance_sheet(books, as_of=dt.date(2026, 10, 31), basis=ACCRUAL)
    assert accrual.value("2100", st.TOTAL) == D("75.00")
    assert_balanced(accrual)


# ---------------------------------------------------------------- views


@pytest.fixture
def treasurer(books):
    user = get_user_model().objects.create_user(email="t@example.org")
    grant(user, association=books, start=dt.date(2020, 1, 1), title="treasurer")
    return user


def test_views_render_and_note_only_on_per_fund_cash(books, client, treasurer):
    client.force_login(treasurer)
    base = "/money/reports/"
    urls = {
        "income-statement/?start=2026-10-01&end=2026-10-31": "Net income",
        "income-statement/?end=2026-10-31&range=ytd": "Year to date",
        "balance-sheet/?as_of=2026-10-31": "Total liabilities and fund balance",
        "cash-flow/?start=2026-10-01&end=2026-10-31": "Net change in cash",
    }
    for url, text in urls.items():
        for suffix, note in (
            ("", False),
            ("&basis=cash", True),
            ("&basis=cash&fund=reserve", True),
            ("&basis=cash&fund=all", False),
        ):
            resp = client.get(base + url + suffix)
            assert resp.status_code == 200, url
            html = resp.content.decode()
            assert text in html and "style=" not in html, url
            assert (NOTE in html) is note, (url, suffix)
    assert "Income statement" in client.get(base).content.decode()


def test_statements_need_finance_read(books, client):
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    for url in ("income-statement/", "balance-sheet/", "cash-flow/"):
        assert client.get("/money/reports/" + url).status_code == 403, url
        assert client.get("/money/reports/" + url + "?format=csv").status_code == 403, url


# ---------------------------------------------------------------- exports


def _rows_by_label(rows):
    return {r[0]: r[1:] for r in rows if r and r[0]}


def test_statement_exports_match_screen(books, client, treasurer):
    from .test_export import csv_rows, pdf_text, xlsx_rows

    client.force_login(treasurer)
    base = "/money/reports/"
    rng = "?start=2026-10-01&end=2026-10-31&basis=cash&fund=all"
    cases = {
        "income-statement/" + rng: ("income-statement", "Net income (loss)"),
        "balance-sheet/?as_of=2026-10-31&basis=cash&fund=all": ("balance-sheet", "Total assets"),
        "cash-flow/" + rng: ("cash-flow", "Net change in cash"),
    }
    for url, (stem, label) in cases.items():
        html = client.get(base + url).content.decode()
        assert 'data-export="xlsx"' in html and "format=pdf" in html, url
        resp = client.get(base + url + "&format=csv")
        assert resp.status_code == 200, url
        assert stem in resp["Content-Disposition"], url
        assert label in _rows_by_label(csv_rows(resp.content)), url
        x = xlsx_rows(client.get(base + url + "&format=xlsx").content)
        assert label in _rows_by_label(x), url
        pdf = client.get(base + url + "&format=pdf").content
        assert pdf.startswith(b"%PDF") and label in pdf_text(pdf), url
    # cash-basis all-funds net income ties to bank movement
    rows = csv_rows(client.get(base + "income-statement/" + rng + "&format=csv").content)
    assert D(_rows_by_label(rows)["Net income (loss)"][0].replace(",", "")) == D("475.00")


def test_statement_export_carries_cash_fund_note(books, client, treasurer):
    from .test_export import csv_rows

    client.force_login(treasurer)
    base = "/money/reports/"
    for url in (
        "income-statement/?start=2026-10-01&end=2026-10-31",
        "balance-sheet/?as_of=2026-10-31",
        "cash-flow/?start=2026-10-01&end=2026-10-31",
    ):
        for suffix, note in (
            ("&basis=cash", True),
            ("&basis=cash&fund=reserve", True),
            ("&basis=cash&fund=all", False),
            ("", False),
        ):
            rows = csv_rows(client.get(base + url + suffix + "&format=csv").content)
            assert any(NOTE in r[0] for r in rows if r) is note, (url, suffix)
            if note:
                assert rows[-1][0].startswith(NOTE), (url, suffix)


def test_statement_export_by_fund_has_fund_columns(books):
    from homeoa.reports import tables

    inc = st.income_statement(
        books, start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 31), basis=ACCRUAL,
        compare=False,
    )  # fmt: skip
    spec = tables.statement(
        inc, title="Income statement", filename="income-statement", period="Oct",
        scope="All funds", basis=ACCRUAL,
    )  # fmt: skip
    assert str(spec.columns[0].label) == "Line"
    assert len(spec.columns) == len(inc.columns) + 1 > 2
    assert all(len(r) == len(spec.columns) for r in spec.rows)
    assert spec.subtitle == "Oct · All funds · Accrual basis"
