"""Master insurance policy: Documents > Insurance, the renewal on the compliance calendar,
and the owners' summary on their Docs tab."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import open_residents
from homeoa.compliance import calendar as cal
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents import services as docs
from homeoa.documents.models import Visibility
from homeoa.insurance import services
from homeoa.insurance.models import MasterPolicy
from homeoa.plugins import registry as plugin_registry

User = get_user_model()
SECRETARY_EMAIL = "secretary@cedarrow.example"
TREASURER_EMAIL = "treasurer@cedarrow.example"
OWNER_EMAIL = "ownerc@cedarrow.example"
TENANT_EMAIL = "chris.doyle@cedarrow.example"
TODAY = dt.date(2026, 10, 7)


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


def test_current_term_and_renewal_on_the_calendar(pilot):
    policy = services.current(pilot, TODAY)
    assert policy.carrier.startswith("Evergreen Mutual")
    rows = [e for e in cal.entries(pilot, TODAY, dt.date(2027, 6, 1)) if e.kind == "insurance"]
    assert [e.due for e in rows] == [dt.date(2027, 3, 1)]
    services.save(
        MasterPolicy(
            association=pilot,
            carrier="Next Carrier",
            start_date=dt.date(2027, 3, 1),
            end_date=dt.date(2028, 3, 1),
        )
    )
    rows = [e for e in cal.entries(pilot, TODAY, dt.date(2027, 6, 1)) if e.kind == "insurance"]
    assert rows == []  # the next term is on file


def test_renewal_reminds_the_board(pilot):
    sent = cal.send_reminders(pilot, dt.date(2027, 2, 22))  # 7 days before the term ends
    assert sent >= 1


def test_term_must_end_after_it_starts(pilot):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        services.save(
            MasterPolicy(
                association=pilot,
                carrier="X",
                start_date=dt.date(2027, 3, 1),
                end_date=dt.date(2027, 3, 1),
            )
        )


def test_secretary_adds_a_term(pilot, client):
    client.force_login(User.objects.get(email=SECRETARY_EMAIL))
    page = client.get(reverse("insurance:index"))
    assert page.status_code == 200 and "Evergreen Mutual" in page.content.decode()
    resp = client.post(
        reverse("insurance:add"),
        {
            "carrier": "Cascade Insurance",
            "start_date": "2027-03-01",
            "end_date": "2028-03-01",
            "deductible": "15000.00",
        },
    )
    assert resp.status_code == 302
    assert services.current(pilot, dt.date(2027, 4, 1)).deductible == Decimal("15000.00")


def test_other_board_members_read_only(pilot, client):
    client.force_login(User.objects.get(email=TREASURER_EMAIL))
    assert client.get(reverse("insurance:index")).status_code == 200
    assert client.get(reverse("insurance:add")).status_code == 403


def test_owner_sees_the_summary_but_not_the_premium(pilot, client):
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    body = client.get(reverse("portal:docs")).content.decode()
    assert 'data-testid="owner-insurance"' in body
    assert "$10,000.00" in body  # deductible
    assert "$10,717.00" not in body  # premium stays with the board
    assert "Certificate of insurance" in body


def test_certificate_hidden_when_owners_cannot_open_it(pilot, client):
    policy = services.current(pilot, TODAY)
    docs.set_visibility(policy.certificate, Visibility.BOARD)
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    body = client.get(reverse("portal:docs")).content.decode()
    assert 'data-testid="owner-insurance"' in body and "Certificate of insurance" not in body


def test_tenants_get_no_summary(pilot, client):
    client.force_login(User.objects.get(email=TENANT_EMAIL))
    body = client.get(reverse("portal:docs")).content.decode()
    assert 'data-testid="owner-insurance"' not in body
