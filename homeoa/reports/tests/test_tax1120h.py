"""Form 1120-H worksheet: income and spending tests, taxable income and tax from the ledger."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.plugins import registry as plugin_registry
from homeoa.reports import cpa, tax1120h

User = get_user_model()
D = Decimal
TREASURER = "treasurer@cedarrow.example"
SECRETARY = "secretary@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def test_tax_after_deductions_at_the_pack_rate():
    ws = tax1120h.Worksheet(
        fy=2026,
        start=dt.date(2026, 1, 1),
        end=dt.date(2026, 12, 31),
        deductions=D("200.00"),
        specific_deduction=D("100.00"),
        rate=D("30"),
        exempt_min_pct=D("60"),
        expenditure_min_pct=D("90"),
    )
    ws.income = [
        tax1120h.Line(None, D("9000.00"), tax1120h.EXEMPT),
        tax1120h.Line(None, D("1000.00"), tax1120h.TAXABLE),
        tax1120h.Line(None, D("500.00"), tax1120h.EXCLUDED),
    ]
    ws.expenses = [
        tax1120h.Line(None, D("950.00"), tax1120h.PROPERTY),
        tax1120h.Line(None, D("50.00"), tax1120h.OTHER),
    ]
    assert ws.gross_income == D("10000.00") and ws.exempt_pct == D("90.0")
    assert ws.income_test is True and ws.spending_test is True
    assert ws.taxable_income == D("700.00") and ws.tax == D("210.00")
    ws.income[1].amount = D("50.00")
    assert ws.taxable_income == D("0.00") and ws.tax == D("0.00")  # never below zero


def test_demo_year_defaults_and_pack_values(pilot):
    ws = tax1120h.worksheet(pilot, 2026)
    classes = {ln.account.code: ln.cls for ln in ws.income}
    assert classes["4000"] == tax1120h.EXEMPT
    assert classes.get("4900", tax1120h.EXCLUDED) == tax1120h.EXCLUDED
    assert ws.exempt_income > 0
    assert (ws.exempt_min_pct, ws.expenditure_min_pct) == (D("60"), D("90"))
    assert ws.specific_deduction == D("100.00") and ws.rate == D("30")


def test_treasurer_classifies_and_others_read(pilot, client):
    url = reverse("reports:tax_1120h")
    client.force_login(User.objects.get(email=TREASURER))
    assert client.get(url, {"year": 2026}).status_code == 200
    client.post(url, {"year": "2026", "inc-4000": "taxable", "deductions": "25.00"})
    pilot.refresh_from_db()
    assert tax1120h.income_class(pilot, "4000") == tax1120h.TAXABLE
    assert tax1120h.worksheet(pilot, 2026).deductions == D("25.00")
    client.force_login(User.objects.get(email=SECRETARY))
    assert client.get(url).status_code == 403


def test_worksheet_is_in_the_cpa_package(pilot):
    names = [cpa.file_name(s, 2026) for s in cpa.specs(pilot, 2026)]
    assert "form-1120-h-worksheet-2026.csv" in names


def test_cpa_reads_the_worksheet_without_a_form(pilot, client):
    from homeoa.access.testing import grant

    cpa = User.objects.create_user("cpa@example.org")
    grant(cpa, "cpa", association=pilot)
    client.force_login(cpa)
    page = client.get(reverse("reports:tax_1120h"), {"year": 2026})
    assert page.status_code == 200 and 'method="post"' not in page.content.decode()
