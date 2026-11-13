"""Owner dashboard and tenant limited view on /me/ (POR-01, REG-06)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from homeoa.access.testing import open_residents
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.registry import services as reg
from homeoa.registry.models import Person, Unit

User = get_user_model()
OCT = dt.date(2026, 10, 1)


@pytest.fixture
def pilot(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    a = demo.seed()
    with association_context(a):
        if not Charge.objects.filter(period=OCT).exists():
            generate_assessments(AssessmentSchedule.objects.get(), OCT, source="test")
        open_residents(a)
        yield a


def _october(email):
    """The Oct charge of the unit this seed user owns (pilot amount)."""
    usr = User.objects.get(email=email)
    unit = reg.units_owned_by(Person.objects.get(user=usr))[0]
    return unit, Charge.objects.get(unit=unit, period=OCT)


def _login(client, email):
    client.force_login(User.objects.get(email=email))


def test_owner_c_sees_october_dues(client, pilot):
    _, charge = _october("ownerc@cedarrow.example")
    assert charge.due_date == dt.date(2026, 10, 10)
    _login(client, "ownerc@cedarrow.example")
    html = client.get("/me/").content.decode()
    assert f'data-testid="balance">${charge.amount:,}<' in html
    assert "Due Sat, Oct 10" in html
    assert 'data-testid="recent-activity"' in html
    assert 'data-testid="quick-links"' in html
    assert 'data-testid="tenant-note"' not in html


def test_tenant_sees_no_money_and_finance_403(client, pilot):
    _login(client, "chris.doyle@cedarrow.example")
    resp = client.get("/me/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert 'data-testid="tenant-note"' in html
    assert 'data-testid="balance"' not in html
    assert "$" not in html
    for url in ("/me/pay/", "/me/payments/", "/me/statements/", "/money/"):
        assert client.get(url).status_code == 403, url


def test_multi_unit_owner_switches_units(client, pilot):
    usr = User.objects.get(email="ownerc@cedarrow.example")
    person = Person.objects.get(user=usr)
    extra = Unit.objects.create(number="Z", allocated_interest=Decimal("0"))
    reg.set_initial_ownership(extra, [person], dt.date(2020, 1, 1))
    client.force_login(usr)
    html = client.get("/me/").content.decode()
    assert 'data-testid="unit-switcher"' in html
    html = client.get("/me/?unit=Z").content.decode()
    assert 'data-testid="balance">$0.00<' in html
    assert "all paid up" in html


def test_render_home_read_only_for_preview(pilot):
    from homeoa.portal.views.home import render_home

    rf = RequestFactory()
    req = rf.get("/me/")
    req.user = User.objects.get(email="ownera@cedarrow.example")
    req.association = pilot
    unit, charge = _october("ownerc@cedarrow.example")
    html = render_home(req, unit=unit, read_only=True, show_money=True).content.decode()
    assert 'data-testid="read-only-note"' in html
    assert f"${charge.amount:,}" in html
    assert 'data-testid="quick-links"' not in html
    assert "Pay $" not in html
