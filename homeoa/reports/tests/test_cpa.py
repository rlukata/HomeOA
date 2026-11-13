# ruff: noqa: F811  (fixtures imported from test_reports are used as arguments)
import csv
import datetime as dt
import io
import zipfile
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.reports import cpa

from .test_reports import assoc, bill, books, pay_bill, today, treasurer  # noqa: F401

D = Decimal
FILES = [
    "trial-balance-accrual-2026.csv",
    "trial-balance-cash-2026.csv",
    "general-ledger-2026.csv",
    "bank-reconciliations-2026.csv",
    "receivables-aging-2026.csv",
    "payables-aging-2026.csv",
    "income-statement-2026.csv",
    "balance-sheet-2026.csv",
    "1099-vendors-2026.csv",
    "form-1120-h-worksheet-2026.csv",
]


def rows(body: bytes):
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


def unzip(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


@pytest.fixture
def ten99_books(books, treasurer):
    from homeoa.vendors.models import Vendor

    with association_context(books):
        b = bill(treasurer, "Rainier Plumbing", "300.00", dt.date(2026, 2, 1), dt.date(2026, 3, 1))
        pay_bill(treasurer, b, dt.date(2026, 2, 20))
        Vendor.objects.filter(name="Rainier Plumbing").update(
            is_1099_eligible=True, legal_name="Rainier Plumbing LLC", tin="911234567"
        )
    return books


def test_zip_file_list_and_audit(client, ten99_books, treasurer, today):
    client.force_login(treasurer)
    resp = client.get(reverse("reports:cpa_package"), {"year": 2026, "download": 1})
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/zip"
    files = unzip(resp.content)
    assert list(files) == FILES
    ev = AuditEvent.objects.get(action="reports.cpa_package_downloaded")
    assert ev.actor == treasurer and ev.changes["year"] == 2026


def test_tb_and_1099_match_screens(client, ten99_books, treasurer, today):
    client.force_login(treasurer)
    files = unzip(client.get(reverse("reports:cpa_package"), {"year": 2026, "download": 1}).content)
    screen = client.get(
        reverse("reports:trial_balance"), {"as_of": "2026-12-31", "format": "csv"}
    ).content
    assert rows(files["trial-balance-accrual-2026.csv"])[1:] == rows(screen)[1:]
    t99 = rows(files["1099-vendors-2026.csv"])
    assert t99[1][:2] == ["Rainier Plumbing", "Rainier Plumbing LLC"]
    assert "4567" in t99[1][2] and "911" not in t99[1][2]
    assert t99[1][4] == "300.00" and t99[-1][-2] == "300.00"


def test_package_specs_scoped(ten99_books):
    with association_context(ten99_books):
        _name, _body, names = cpa.build(ten99_books, 2026)
    assert names == FILES


def test_plain_board_member_cannot_download(client, books):
    user = get_user_model().objects.create_user(email="b@example.org")
    grant(user, association=books, start=dt.date(2020, 1, 1))
    client.force_login(user)
    assert client.get(reverse("reports:cpa_package")).status_code == 403


def test_page_renders(client, books, treasurer):
    client.force_login(treasurer)
    resp = client.get(reverse("reports:cpa_package"))
    assert resp.status_code == 200
    assert b'data-testid="cpa-download"' in resp.content
