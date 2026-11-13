"""Vendors list Contract, Insurance certificate and Paid this year columns."""

import datetime as dt
import re
from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from homeoa.access.testing import grant
from homeoa.ap.models import Bill, BillLine
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.vendors import alerts, services
from homeoa.vendors.models import Contract, Vendor, VendorDocument

TODAY = dt.date(2026, 10, 8)
START = dt.date(2020, 1, 1)


@pytest.fixture(autouse=True)
def _clock(settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def vendor(assoc):
    return services.save_vendor(Vendor(name="Greenway Landscaping", category="Grounds"))


@pytest.fixture
def client_in(client, assoc):
    user = get_user_model().objects.create_user(email="t@example.org")
    grant(user, association=assoc, start=START, title="treasurer")
    client.force_login(user)
    return client


def _cell(html, kind, slug):
    m = re.search(rf'data-testid="vendor-{kind}-{slug}">(.*?)</td>', html, re.S)
    assert m, f"no {kind} cell"
    return m.group(1)


def _list(client):
    return client.get("/vendors/").content.decode()


def _contract(vendor, end, auto_renew=False, notice_days=0):
    return Contract.objects.create(
        vendor=vendor,
        title="Grounds",
        start_date=end - dt.timedelta(days=365),
        end_date=end,
        auto_renew=auto_renew,
        notice_days=notice_days,
    )


def _coi(vendor, expiry):
    return VendorDocument.objects.create(vendor=vendor, kind="coi", expiry_date=expiry)


def _bill(vendor, amount, status=Bill.Status.PAID, paid_on=TODAY, number="INV-1"):
    bill = Bill.objects.create(
        vendor=vendor,
        invoice_number=number,
        bill_date=paid_on or TODAY,
        status=status,
        paid_on=paid_on,
    )
    BillLine.objects.create(
        bill=bill,
        account=Account.objects.get(code="5000"),
        fund=Fund.objects.get(code="operating"),
        amount=D(amount),
    )


def test_empty_columns(client_in, vendor):
    html = _list(client_in)
    assert "—" in _cell(html, "contract", vendor.slug)
    assert "None on file" in _cell(html, "coi", vendor.slug)
    assert "$0.00" in _cell(html, "paid", vendor.slug)
    assert f"vendor-expired-{vendor.slug}" not in html


@pytest.mark.parametrize(
    ("end", "auto", "notice", "kind", "text"),
    [
        (dt.date(2027, 6, 30), True, 30, None, "Auto-renews Jun 30, 2027"),
        (dt.date(2026, 11, 1), True, 30, "warn", "Auto-renews Nov 1, 2026"),
        (dt.date(2026, 11, 1), False, 30, "warn", "Ends Nov 1, 2026"),
        (dt.date(2027, 6, 30), False, 0, None, "Ends Jun 30, 2027"),
        (dt.date(2026, 10, 7), True, 30, "danger", "Ended Oct 7, 2026"),
    ],
)
def test_contract_states(client_in, vendor, end, auto, notice, kind, text):
    _contract(vendor, dt.date(2025, 1, 1))  # an older term is ignored
    _contract(vendor, end, auto, notice)
    cell = _cell(_list(client_in), "contract", vendor.slug)
    assert text in cell
    if kind:
        assert f"badge {kind}" in cell
    else:
        assert "badge" not in cell


@pytest.mark.parametrize(
    ("expiry", "kind", "text"),
    [
        (dt.date(2027, 3, 1), "ok", "Expires Mar 1, 2027"),
        (TODAY + dt.timedelta(days=30), "warn", "Expires Nov 7, 2026"),
        (TODAY, "warn", "Expires Oct 8, 2026"),
        (TODAY - dt.timedelta(days=1), "danger", "Expired Oct 7, 2026"),
    ],
)
def test_coi_states(client_in, vendor, expiry, kind, text):
    _coi(vendor, dt.date(2020, 1, 1))  # superseded
    _coi(vendor, expiry)
    html = _list(client_in)
    cell = _cell(html, "coi", vendor.slug)
    assert text in cell and f"badge {kind}" in cell
    assert (f"vendor-expired-{vendor.slug}" in html) == (kind == "danger")


def test_coi_window_setting(client_in, vendor, settings):
    settings.HOMEOA_VENDOR_DOC_WARNING_DAYS = 10
    _coi(vendor, TODAY + dt.timedelta(days=30))
    assert "badge ok" in _cell(_list(client_in), "coi", vendor.slug)


def test_states_match_todo_job(assoc, vendor):
    doc = _coi(vendor, TODAY + dt.timedelta(days=30))
    contract = _contract(vendor, dt.date(2026, 11, 1), True, 30)
    raised = alerts.run(assoc, TODAY)
    assert {a.document_id or a.contract_id for a in raised} == {doc.pk, contract.pk}
    assert alerts.document_state(doc.expiry_date, TODAY) == "warn"
    assert alerts.contract_state(contract, TODAY) == "warn"
    assert alerts.document_state(doc.expiry_date, TODAY - dt.timedelta(days=1)) == "ok"


def test_paid_this_year(client_in, vendor):
    _bill(vendor, "100.00", number="A")
    _bill(vendor, "25.50", paid_on=dt.date(2026, 1, 1), number="B")
    _bill(vendor, "999.00", paid_on=dt.date(2025, 12, 31), number="C")
    _bill(vendor, "500.00", status=Bill.Status.APPROVED, paid_on=None, number="D")
    assert "$125.50" in _cell(_list(client_in), "paid", vendor.slug)


def test_paid_uses_fiscal_year(assoc, vendor):
    assoc.fiscal_year_start_month = 7
    assoc.save()
    _bill(vendor, "10.00", paid_on=dt.date(2026, 6, 30), number="A")
    _bill(vendor, "20.00", paid_on=dt.date(2026, 7, 1), number="B")
    assert services.paid_by_vendor(assoc, TODAY) == {vendor.pk: D("20.00")}


def test_query_count_bounded(client_in, assoc):
    for i in range(10):
        v = services.save_vendor(Vendor(name=f"Vendor {i}"))
        _contract(v, dt.date(2027, 1, 1))
        _coi(v, dt.date(2027, 1, 1))
        _bill(v, "1.00", number=f"N{i}")
    client_in.get("/vendors/")
    with CaptureQueriesContext(connection) as ctx:
        resp = client_in.get("/vendors/")
    assert resp.status_code == 200
    first = len(ctx.captured_queries)
    v = services.save_vendor(Vendor(name="Vendor 11"))
    _coi(v, dt.date(2027, 1, 1))
    with CaptureQueriesContext(connection) as ctx:
        client_in.get("/vendors/")
    assert len(ctx.captured_queries) == first


def test_htmx_rows_keep_columns(client_in, vendor):
    _coi(vendor, TODAY - dt.timedelta(days=1))
    html = client_in.get("/vendors/?q=Green", HTTP_HX_REQUEST="true").content.decode()
    assert html.lstrip().startswith("<tbody")
    assert f"vendor-coi-{vendor.slug}" in html and f"vendor-paid-{vendor.slug}" in html


def test_association_isolation(client_in, assoc, vendor):
    other = Association.objects.create(name="Other", short_name="OT")
    load_chart_of_accounts(other)
    with association_context(other):
        ov = services.save_vendor(Vendor(name="Greenway Landscaping"))
        _coi(ov, TODAY - dt.timedelta(days=5))
        _bill(ov, "777.00")
    html = _list(client_in)
    assert "None on file" in _cell(html, "coi", vendor.slug)
    assert "$777.00" not in html
    assert services.paid_by_vendor(assoc, TODAY) == {}


@pytest.mark.parametrize(("days", "flag"), [(-1, True), (0, False), (10, False)])
def test_license_expired_flag(client_in, vendor, days, flag):
    VendorDocument.objects.create(vendor=vendor, kind="license", expiry_date=dt.date(2020, 1, 1))
    VendorDocument.objects.create(
        vendor=vendor, kind="license", expiry_date=TODAY + dt.timedelta(days=days)
    )
    html = _list(client_in)
    assert (f"vendor-license-expired-{vendor.slug}" in html) == flag
    assert "None on file" in _cell(html, "coi", vendor.slug)
