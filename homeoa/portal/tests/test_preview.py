"""Board preview-as-unit. Units come from seed users, never hardcoded."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import services
from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person

User = get_user_model()
OCT = dt.date(2026, 10, 1)
BOARD = "treasurer@cedarrow.example"


@pytest.fixture
def pilot(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    a = demo.seed()
    with association_context(a):
        if not Charge.objects.filter(period=OCT).exists():
            generate_assessments(AssessmentSchedule.objects.get(), OCT, source="test")
        yield a


def _unit_of(email):
    return reg.units_owned_by(Person.objects.get(user__email=email))[0]


def _board(client):
    client.force_login(User.objects.get(email=BOARD))


def _url(unit, rest=""):
    return f"/units/{unit.number}/preview/{rest}"


def test_preview_owner_dashboard_read_only_while_residents_off(client, pilot):
    assert not services.residents_enabled(pilot)
    unit = _unit_of("ownerc@cedarrow.example")
    charge = Charge.objects.get(unit=unit, period=OCT)
    _board(client)
    resp = client.get(_url(unit))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'data-testid="preview-banner"' in html
    assert f'data-testid="balance">${charge.amount:,}<' in html
    assert "Pay $" not in html
    assert 'data-testid="quick-links"' not in html
    # Tabs stay inside the preview, Pay included for the owner persona.
    assert f'href="{_url(unit, "pay/")}"' in html
    assert 'href="/me/pay/"' not in html
    assert client.get(_url(unit, "docs/")).status_code == 200


def test_preview_refuses_post(client, pilot):
    unit = _unit_of("ownerc@cedarrow.example")
    _board(client)
    assert client.post(_url(unit)).status_code == 405
    assert client.post(_url(unit, "pay/")).status_code == 405


def test_preview_audited_once_per_session(client, pilot):
    unit = _unit_of("ownerc@cedarrow.example")
    _board(client)
    client.get(_url(unit))
    client.get(_url(unit))
    client.get(_url(unit, "docs/"))
    assert AuditEvent.objects.filter(action="unit.preview").count() == 1
    client.get(_url(unit) + "?as=tenant")
    assert AuditEvent.objects.filter(action="unit.preview").count() == 2


def test_tenant_persona_hides_money(client, pilot):
    occ = Occupancy.objects.filter(kind=Occupancy.Kind.TENANT).select_related("unit").first()
    unit = occ.unit
    _board(client)
    html = client.get(_url(unit) + "?as=tenant").content.decode()
    assert 'data-testid="tenant-note"' in html
    assert 'data-testid="balance"' not in html
    assert _url(unit, "pay/") not in html
    assert client.get(_url(unit, "pay/") + "?as=tenant").status_code == 404


def test_resident_cannot_preview(client, pilot):
    open_residents(pilot)
    unit = _unit_of("ownerc@cedarrow.example")
    client.force_login(User.objects.get(email="ownera@cedarrow.example"))
    assert client.get(_url(unit)).status_code == 403


def test_unit_page_links_to_preview(client, pilot):
    unit = _unit_of("ownerc@cedarrow.example")
    _board(client)
    html = client.get(f"/units/{unit.number}/").content.decode()
    assert f"Preview as unit {unit.number}" in html
    assert f'href="{_url(unit)}"' in html
