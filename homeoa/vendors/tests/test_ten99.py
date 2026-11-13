# ruff: noqa: F811  (fixtures imported from reports tests are used as arguments)
"""Vendors > 1099 tracking: cash paid by payment date, eligible vendors only."""

import datetime as dt
import io
import zipfile
from decimal import Decimal

import pytest
from django.urls import reverse

from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.reports.tests.test_reports import assoc, bill, pay_bill, treasurer  # noqa: F401
from homeoa.vendors import services as vendor_services
from homeoa.vendors import ten99
from homeoa.vendors.models import Vendor

D = Decimal


def eligible(name, **kw):
    Vendor.objects.filter(name=name).update(is_1099_eligible=True, **kw)


@pytest.fixture
def paid(assoc, treasurer):
    # Billed in 2025, paid in 2026: counts in 2026.
    b1 = bill(treasurer, "Rainier Plumbing", "300.00", dt.date(2025, 12, 1))
    pay_bill(treasurer, b1, dt.date(2026, 1, 5))
    # Billed in 2026, paid in 2027: not 2026.
    b2 = bill(treasurer, "Sound Roofing", "900.00", dt.date(2026, 12, 1))
    pay_bill(treasurer, b2, dt.date(2027, 1, 3))
    # Paid in 2026 but not eligible.
    b3 = bill(treasurer, "Puget Power", "50.00", dt.date(2026, 3, 1))
    pay_bill(treasurer, b3, dt.date(2026, 3, 20))
    # Eligible, approved but unpaid.
    bill(treasurer, "Evergreen Paint", "75.00", dt.date(2026, 4, 1))
    eligible("Rainier Plumbing", legal_name="Rainier Plumbing LLC", w9_received=dt.date(2026, 1, 2))
    eligible("Sound Roofing")
    eligible("Evergreen Paint")
    return assoc


def by_name(t):
    return {r.vendor.name: r for r in t.rows}


def test_totals_by_payment_date(paid):
    t26 = by_name(ten99.totals(paid, 2026))
    assert set(t26) == {"Rainier Plumbing", "Sound Roofing", "Evergreen Paint"}
    assert t26["Rainier Plumbing"].paid == D("300.00")
    assert t26["Sound Roofing"].paid == D("0.00")
    assert t26["Evergreen Paint"].paid == D("0.00")
    assert ten99.totals(paid, 2025).total == D("0.00")
    t27 = ten99.totals(paid, 2027)
    assert t27.total == D("900.00")
    assert [r.vendor.name for r in t27.missing_w9] == ["Sound Roofing"]


def test_association_scoped(paid):
    other = Association.objects.create(name="Other", short_name="O")
    load_chart_of_accounts(other)
    with association_context(other):
        vendor_services.save_vendor(Vendor(name="Elsewhere", is_1099_eligible=True))
    assert "Elsewhere" not in by_name(ten99.totals(paid, 2026))
    assert [r.vendor.name for r in ten99.totals(other, 2026).rows] == ["Elsewhere"]


def test_no_threshold_by_default(settings):
    assert ten99.threshold() is None
    settings.HOMEOA_1099_THRESHOLD = "600"
    assert ten99.threshold() == D("600")


def test_tab_renders_and_csv_matches_zip(client, paid, treasurer):
    client.force_login(treasurer)
    resp = client.get(reverse("vendors:ten99"), {"year": 2027})
    page = resp.content.decode()
    assert resp.status_code == 200
    assert 'data-testid="ten99-missing-w9"' in page
    assert "Puget Power" not in page
    assert reverse("vendors:ten99") in page  # the tab is in the hub nav
    csv_body = client.get(reverse("vendors:ten99"), {"year": 2026, "format": "csv"}).content
    with association_context(paid):
        zip_body = client.get(reverse("reports:cpa_package"), {"year": 2026, "download": 1}).content
    with zipfile.ZipFile(io.BytesIO(zip_body)) as zf:
        assert zf.read("1099-vendors-2026.csv") == csv_body


def test_card_and_app_payments_left_out_for_1099k(paid, treasurer):
    from homeoa.ap.models import Bill

    b = bill(treasurer, "Rainier Plumbing", "120.00", dt.date(2026, 5, 1))
    pay_bill(treasurer, b, dt.date(2026, 5, 10))
    vendor = Vendor.objects.get(name="Rainier Plumbing", is_1099_eligible=True)
    Bill.all_associations.filter(pk=b.pk).update(
        vendor=vendor, invoice_number="card-1", paid_by=Bill.PaidBy.CARD
    )
    row = by_name(ten99.totals(paid, 2026))["Rainier Plumbing"]
    assert row.paid == D("300.00")  # the card payment is the processor's to report
    assert row.by_processor == D("120.00")
    assert ten99.totals(paid, 2026).by_processor == D("120.00")
