"""Resident directory: owners and the board see the roster with the records-export redaction."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import open_residents
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.plugins import registry as plugin_registry
from homeoa.registry import directory
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, Unit

User = get_user_model()
BOARD_EMAIL = "treasurer@cedarrow.example"
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


@pytest.fixture
def owner(pilot):
    person = reg.current_owners(Unit.objects.get(number="A"))[0]
    person.email = "owner@example.test"
    person.phone = "206-555-0100"
    person.save()
    return person


def _person(pilot, name):
    for group in directory.units(pilot, TODAY):
        for p in group["people"]:
            if p["name"] == name:
                return p
    raise AssertionError(f"{name} not listed")


def test_directory_lists_owner_contact(pilot, owner):
    p = _person(pilot, owner.full_name)
    assert p["role"] == "Owner"
    assert p["email"] == "owner@example.test" and p["phone"] == "206-555-0100"
    assert p["address"] == ""  # lives at the unit, so no separate address


def test_directory_leaves_out_what_the_export_leaves_out(pilot, owner):
    owner.phone_unlisted = owner.email_confidential = True
    owner.acp_participant = True
    owner.acp_address = "PO Box 257"
    owner.save()
    tenant = Person.objects.create(association=pilot, full_name="Tia Tenant", email="t@x.test")
    reg.add_occupancy(owner.ownership_periods.get().unit, tenant, dt.date(2026, 1, 1))
    p = _person(pilot, owner.full_name)
    assert p["email"] == "" and p["phone"] == "" and p["address"] == "Address withheld"
    t = _person(pilot, "Tia Tenant")
    assert t["role"] == str(Occupancy.Kind.TENANT.label) and t["email"] == ""


def test_owner_opens_directory_from_more(pilot, owner, client):
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    more = client.get(reverse("portal:more")).content.decode()
    assert reverse("directory:portal") in more
    page = client.get(reverse("directory:portal"))
    assert page.status_code == 200
    assert "owner@example.test" in page.content.decode()


def test_tenant_has_no_directory(pilot, client):
    client.force_login(User.objects.get(email=TENANT_EMAIL))
    assert reverse("directory:portal") not in client.get(reverse("portal:more")).content.decode()
    assert client.get(reverse("directory:portal")).status_code == 403


def test_board_sees_directory_under_community(pilot, owner, client):
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    page = client.get(reverse("directory:board"))
    assert page.status_code == 200
    assert "owner@example.test" in page.content.decode()
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    assert client.get(reverse("directory:board")).status_code == 403


def test_owner_marks_own_phone_unlisted(pilot, client):
    user = User.objects.get(email=OWNER_EMAIL)
    me = Person.objects.get(user=user)
    client.force_login(user)
    data = {f: getattr(me, f) or "" for f in reg.CONTACT_FIELDS if f not in reg.CONTACT_FLAGS}
    data.update(phone="206-555-0142", phone_unlisted="on")
    assert client.post(reverse("portal:profile"), data).status_code == 302
    me.refresh_from_db()
    assert me.phone_unlisted
    assert _person(pilot, me.full_name)["phone"] == ""
