"""Itemized ledger export for mediation (RCW 61.24.163(7)(a))."""

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.audit.models import AuditEvent
from homeoa.billing.models import ChargeKind
from homeoa.billing.services.adjustments import manual_charge
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents import mediation
from homeoa.documents.models import LedgerExport
from homeoa.ledger.services import unit_balance
from homeoa.payments.services import record_receipt
from homeoa.plugins import registry
from homeoa.registry.models import Unit

User = get_user_model()
TODAY = dt.date(2026, 10, 7)
BOARD = "treasurer@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def _charge(unit, amount, on, kind=ChargeKind.OTHER):
    return manual_charge(
        unit,
        Decimal(amount),
        "test",
        f"Charge {on}",
        kind=kind,
        date=on,
        due_date=on + dt.timedelta(days=14),
    )


def test_months_back():
    assert mediation.months_back(dt.date(2026, 10, 7)) == dt.date(2025, 10, 7)
    assert mediation.months_back(dt.date(2028, 2, 29)) == dt.date(2027, 2, 28)


def test_range_twelve_months_when_not_long_past_due(pilot):
    unit = Unit.objects.get(number="C")
    _charge(unit, "50.00", dt.date(2025, 6, 1))
    record_receipt(
        unit=unit, amount=Decimal("50.00"), received_date=dt.date(2025, 6, 5), method="check"
    )
    exp = mediation.build(unit)
    assert exp.start == dt.date(2025, 10, 7)
    # The paid June 2025 charge is before the window: carried in the balance forward.
    assert all(i.date >= exp.start for i in exp.items)


def test_range_since_past_due_when_longer(pilot):
    unit = Unit.objects.get(number="C")
    _charge(unit, "75.00", dt.date(2025, 3, 1), kind=ChargeKind.SPECIAL)
    exp = mediation.build(unit)
    assert exp.start == dt.date(2025, 3, 1)
    assert exp.past_due_since == dt.date(2025, 3, 15)
    assert exp.items[0].type == "Special assessment"
    assert exp.items[0].charge == Decimal("75.00")


def test_totals_equal_subledger(pilot):
    unit = Unit.objects.get(number="C")
    _charge(unit, "75.00", dt.date(2024, 3, 1))
    _charge(unit, "20.00", dt.date(2026, 9, 20))
    record_receipt(
        unit=unit, amount=Decimal("10.00"), received_date=dt.date(2026, 9, 25), method="check"
    )
    exp = mediation.build(unit)
    assert exp.balance == unit_balance(
        unit.pk, as_of=TODAY, ownership_period_id=exp.period.pk, association=pilot
    )
    net = sum((i.charge - i.payment for i in exp.items), Decimal("0"))
    assert exp.opening + net == exp.balance
    assert any(i.type == "Payment" and i.payment == Decimal("10.00") for i in exp.items)


def test_csv_columns(pilot):
    unit = Unit.objects.get(number="C")
    exp = mediation.build(unit)
    rows = list(csv.reader(io.StringIO(mediation.to_csv(exp).decode())))
    assert rows[0] == ["date", "type", "description", "charge", "payment", "balance"]
    assert rows[1][1] == "Balance forward"
    assert rows[-1][1] == "Total balance"
    assert rows[-1][5] == f"{exp.balance:.2f}"
    assert len(rows) == len(exp.items) + 3


def test_export_is_filed_audited_and_downloadable(client, pilot):
    client.force_login(User.objects.get(email=BOARD))
    resp = client.post("/units/C/file/ledger-export/")
    assert resp.status_code == 302
    rec = LedgerExport.objects.get()
    assert AuditEvent.objects.filter(action="unit.ledger_export", object_id=str(rec.pk)).exists()
    html = client.get("/units/C/file/").content.decode()
    assert 'data-kind="ledger_exports"' in html and 'data-testid="ledger-export"' in html
    pdf = client.get(f"/unit-file/ledger-exports/{rec.pk}/pdf/")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    c = client.get(f"/unit-file/ledger-exports/{rec.pk}/csv/")
    assert c.status_code == 200 and c.content.startswith(b"date,type")
    assert client.get(f"/unit-file/ledger-exports/{rec.pk}/xls/").status_code == 404


def test_permission(client, pilot):
    client.force_login(User.objects.get(email="ownerc@cedarrow.example"))
    assert client.post("/units/C/file/ledger-export/").status_code in (302, 403)  # owners: /me/
    assert not LedgerExport.objects.exists()
    rec = mediation.export(Unit.objects.get(number="C"))
    assert client.get(f"/unit-file/ledger-exports/{rec.pk}/csv/").status_code in (302, 403)
    assert client.get("/units/C/file/ledger-export/").status_code in (302, 403, 405)


def test_fine_charge_labelled_fine(pilot):
    unit = Unit.objects.get(number="C")
    _charge(unit, "50.00", dt.date(2026, 9, 20), kind=ChargeKind.FINE)
    exp = mediation.build(unit)
    assert any(i.type == "Fine" and i.charge == Decimal("50.00") for i in exp.items)
    assert not any(i.type == "Other charge" for i in exp.items)
