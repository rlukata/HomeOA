"""Resident Requests tab (POR-04) and the complete unit file (DOC-02)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant, open_residents
from homeoa.arc import services as arc_services
from homeoa.arc.models import ArcForm, ArcRequest
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents import unitfile
from homeoa.enforcement.models import Violation, ViolationCategory
from homeoa.maintenance import services as maint
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import current_owners
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

User = get_user_model()
BOARD = "treasurer@cedarrow.example"
OWNER_A = "ownera@cedarrow.example"
TENANT_E = "chris.doyle@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    a = demo.seed()
    with association_context(a):
        yield a


def _arc(unit, title, on, status="submitted", decided=None):
    arc_services.ensure_default_form(unit.association)
    owner = current_owners(unit, dt.date(2026, 10, 7))[0]
    return ArcRequest.objects.create(
        unit=unit,
        submitted_by=owner,
        form=ArcForm.objects.first(),
        title=title,
        submitted_on=on,
        status=status,
        decided_on=decided,
    )


def _maint(unit, title, email):
    return maint.submit_request(
        title=title, unit=unit, submitted_by=Person.objects.get(user__email=email)
    )


@pytest.fixture
def data(pilot):
    a, e = Unit.objects.get(number="A"), Unit.objects.get(number="E")
    arc = _arc(a, "New front door", dt.date(2026, 9, 1), "approved", dt.date(2026, 9, 20))
    m_a = _maint(a, "Leaky gutter", OWNER_A)
    m_e = _maint(e, "Bathroom fan rattles", TENANT_E)
    cat = ViolationCategory.objects.create(name="Noise", rule_citation="Rules 4.2")
    vio = Violation.objects.create(
        unit=a, category=cat, observed_on=dt.date(2026, 8, 15), description="Loud music"
    )
    vendor = vendor_services.save_vendor(Vendor(name="Evergreen Gutters"))
    wo = maint.create_work_order(vendor=vendor, scope="Clean gutters", request=m_a)
    return {"arc": arc, "m_a": m_a, "m_e": m_e, "vio": vio, "wo": wo}


def _login(client, email):
    client.force_login(User.objects.get(email=email))


def _on(pilot):
    open_residents(pilot)


def test_owner_sees_arc_and_maintenance_with_chooser(client, pilot, data):
    _on(pilot)
    _login(client, OWNER_A)
    html = client.get("/me/requests/").content.decode()
    assert "New front door" in html and "Leaky gutter" in html
    assert "Bathroom fan rattles" not in html  # another resident's request
    assert f'href="/me/arc/{data["arc"].pk}/"' in html
    assert f'href="/me/maintenance/{data["m_a"].pk}/"' in html
    assert 'data-testid="req-new-arc"' in html and 'data-testid="req-new-maint"' in html
    assert "Sep 20 · Approved" in html  # timeline
    # Newest first: the maintenance request (today) before the September ARC request.
    assert html.index("Leaky gutter") < html.index("New front door")


def test_tenant_sees_maintenance_only(client, pilot, data):
    _on(pilot)
    _login(client, TENANT_E)
    html = client.get("/me/requests/").content.decode()
    assert "Bathroom fan rattles" in html
    assert 'data-kind="arc"' not in html
    assert 'data-testid="req-new-arc"' not in html
    assert 'data-testid="req-new-maint"' in html


def test_tab_closed_until_cutover(client, pilot, data):
    _login(client, OWNER_A)
    resp = client.get("/me/requests/")
    assert resp.status_code == 403
    assert "New front door" not in resp.content.decode()


def test_board_preview_as_unit(client, pilot, data):
    _login(client, BOARD)
    html = client.get("/units/A/preview/requests/").content.decode()
    assert "New front door" in html and "Leaky gutter" in html
    assert 'aria-disabled="true"' in html and 'href="/me/arc/' not in html
    tenant = client.get("/units/E/preview/requests/?as=tenant").content.decode()
    assert "Bathroom fan rattles" in tenant and 'data-kind="arc"' not in tenant


class _Req:
    def __init__(self, user, association):
        self.user, self.association = user, association


def _kinds(unit, user, pilot):
    return [e.kind for e in unitfile.entries(unit, _Req(user, pilot))]


def test_unit_file_board_sees_every_contributor_by_date(client, pilot, data):
    a = Unit.objects.get(number="A")
    kinds = _kinds(a, User.objects.get(email=BOARD), pilot)
    for k in ("violations", "arc", "maintenance", "work_orders"):
        assert k in kinds
    entries = unitfile.entries(a, _Req(User.objects.get(email=BOARD), pilot))
    dates = [e.date for e in entries]
    assert dates == sorted(dates, reverse=True)
    _login(client, BOARD)
    html = client.get("/units/A/file/").content.decode()
    assert 'data-kind="violations"' in html and 'data-kind="work_orders"' in html
    assert f"/requests/architectural/{data['arc'].pk}/" in html


def test_unit_file_contributors_gated_per_role(pilot, data):
    a = Unit.objects.get(number="A")
    cpa = User.objects.create_user("cpa@example.com")
    grant(cpa, "cpa", association=pilot)
    committee = User.objects.create_user("arc@example.com")
    grant(committee, "committee", association=pilot)
    _on(pilot)
    owner = User.objects.get(email=OWNER_A)
    new = {"violations", "arc", "maintenance", "work_orders"}
    assert not new & set(_kinds(a, cpa, pilot))
    assert set(_kinds(a, committee, pilot)) & new == {"arc"}
    assert not new & set(_kinds(a, owner, pilot))  # owners see violations on Requests
