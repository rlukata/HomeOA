"""Owner records export with statutory redaction."""

import datetime as dt
import io

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa import rules
from homeoa.access.testing import grant, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos
from homeoa.plugins import registry as plugin_registry
from homeoa.registry import export as rx
from homeoa.registry import records
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, RecordsRequest, Unit

User = get_user_model()
BOARD_EMAIL = "treasurer@cedarrow.example"
C_EMAIL = "ownerc@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


@pytest.fixture
def people(pilot):
    unit = Unit.objects.get(number="A")
    owner = reg.current_owners(unit)[0]
    owner.email = "owner@example.test"
    owner.phone = "206-555-0100"
    owner.address_line1 = "1 Real St"
    owner.city = "Tukwila"
    owner.save()
    tenant = Person.objects.create(
        association=pilot, full_name="Tia Tenant", email="tia@example.test", phone="206-555-0199"
    )
    reg.add_occupancy(unit, tenant, dt.date(2026, 1, 1), Occupancy.Kind.TENANT)
    return {"unit": unit, "owner": owner, "tenant": tenant}


def _row(spec, name):
    return next(r for r in spec.rows if r[3] == name)


def test_owner_mode_keeps_listed_owner_contact(people):
    spec = rx.roster_spec(people["unit"].association)
    row = _row(spec, people["owner"].full_name)
    assert row[0] == "A" and row[2] == "Owner"
    assert row[5].startswith("1 Real St")
    assert row[6] == "owner@example.test" and row[7] == "206-555-0100"
    assert row[4] is not None  # ownership share


def test_unlisted_phone_removed(people):
    people["owner"].phone_unlisted = True
    people["owner"].save()
    row = _row(rx.roster_spec(people["unit"].association), people["owner"].full_name)
    assert row[7] == "" and row[6] == "owner@example.test"


def test_confidential_email_removed(people):
    people["owner"].email_confidential = True
    people["owner"].save()
    row = _row(rx.roster_spec(people["unit"].association), people["owner"].full_name)
    assert row[6] == "" and row[7] == "206-555-0100"


def test_resident_email_always_removed(people):
    row = _row(rx.roster_spec(people["unit"].association), "Tia Tenant")
    assert row[2] == "Tenant" and row[6] == ""


def test_acp_address_withheld_with_its_substitute(people):
    """RCW 64.34.372(6): remove the address; the substitute would reveal participation."""
    owner = people["owner"]
    owner.acp_participant = True
    owner.acp_address = "PO Box 257\nOlympia, WA 98507"
    owner.save()
    row = _row(rx.roster_spec(owner.association), owner.full_name)
    assert row[5] == "Address withheld"
    rows = str(rx.roster_spec(owner.association).rows)
    assert "Real St" not in rows and "PO Box 257" not in rows


def test_full_mode_keeps_everything_and_is_watermarked(people):
    owner = people["owner"]
    owner.phone_unlisted = owner.email_confidential = owner.acp_participant = True
    owner.save()
    spec = rx.roster_spec(owner.association, mode=rx.FULL)
    row = _row(spec, owner.full_name)
    assert row[5].startswith("1 Real St") and row[6] and row[7]
    assert _row(spec, "Tia Tenant")[6] == "tia@example.test"
    assert spec.watermark and "confidential" in spec.filename
    from homeoa.reports import export

    assert export.to_csv(spec).lstrip("﻿").startswith("CONTAINS CONFIDENTIAL DATA")
    assert export.render(spec, export.PDF)[:4] == b"%PDF"


def test_unknown_mode_rejected(people):
    with pytest.raises(ValueError):
        rx.redact(people["owner"], people["unit"], is_owner=True, mode="raw")


def test_all_formats_share_redaction(people, client):
    people["owner"].email_confidential = True
    people["owner"].save()
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    url = reverse("registry:records")
    csv_body = client.get(url, {"format": "csv"}).content.decode()
    assert "owner@example.test" not in csv_body and "tia@example.test" not in csv_body
    from openpyxl import load_workbook

    resp = client.get(url, {"format": "xlsx"})
    wb = load_workbook(io.BytesIO(resp.content))
    cells = [c for row in wb.active.iter_rows(values_only=True) for c in row]
    assert "owner@example.test" not in cells and "tia@example.test" not in cells
    assert client.get(url, {"format": "pdf"})["Content-Type"] == "application/pdf"
    assert AuditEvent.objects.filter(action="reports.exported").count() == 3


def test_page_needs_permission(people, client):
    client.force_login(User.objects.get(email=C_EMAIL))
    assert client.get(reverse("registry:records")).status_code == 403
    assert client.get(reverse("registry:records"), {"format": "csv"}).status_code == 403
    resp = client.post(reverse("registry:records_full"), {"format": "csv", "reason": "x"})
    assert resp.status_code == 403


def test_board_page_renders(people, client):
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    resp = client.get(reverse("registry:records"))
    assert resp.status_code == 200
    assert b'data-testid="full-export"' in resp.content


def test_full_export_needs_reason_and_is_audited(people, client):
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    url = reverse("registry:records_full")
    assert client.post(url, {"format": "csv", "reason": "  "}).status_code == 400
    assert not AuditEvent.objects.filter(action="registry.records_full_export").exists()
    resp = client.post(url, {"format": "csv", "reason": "Hand-over to new manager"})
    assert resp.status_code == 200
    assert "tia@example.test" in resp.content.decode()
    event = AuditEvent.objects.get(action="registry.records_full_export")
    assert event.changes["reason"] == "Hand-over to new manager"


def test_full_export_needs_the_records_permission(people, client):
    """The unredacted roster is for people who keep the records (``units.manage``); the
    system admin and residents get 403."""
    admin = User.objects.create_user("admin@example.org")
    grant(admin, "sys_admin", association=people["unit"].association)
    for user in (admin, User.objects.create_user("nobody@example.org")):
        client.force_login(user)
        resp = client.post(reverse("registry:records_full"), {"format": "csv", "reason": "x"})
        assert resp.status_code == 403


def test_request_deadline_from_rule_pack_and_todo(people, client):
    assoc = people["unit"].association
    assert rules.resolve(assoc, "records.inspection_days").verified is False
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    resp = client.post(
        reverse("registry:records_request_add"),
        {"person": people["owner"].pk, "received_on": "2026-10-07", "note": "Minutes"},
    )
    assert resp.status_code == 302
    req = RecordsRequest.objects.get()
    assert req.due_on == dt.date(2026, 10, 17) and req.latest_on == dt.date(2026, 10, 28)
    items = [i for i in todos.collect(assoc) if i.id == f"records-request-{req.pk}"]
    assert items and items[0].due == dt.date(2026, 10, 17)
    assert "2026-10-28" in items[0].sub
    client.post(reverse("registry:records_request_done", args=[req.pk]))
    req.refresh_from_db()
    assert req.fulfilled_on == dt.date(2026, 10, 7)
    assert not records.todo_items(assoc)


def test_request_needs_owner_or_name(people, client):
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    resp = client.post(reverse("registry:records_request_add"), {"received_on": "2026-10-07"})
    assert resp.status_code == 400


def test_owner_sets_email_confidential_in_portal(people, client):
    me = User.objects.get(email=C_EMAIL)
    person = Person.objects.get(user=me)
    data = {f: getattr(person, f) for f in reg.CONTACT_FIELDS}
    data["email_confidential"] = "on"
    client.force_login(me)
    resp = client.post(reverse("portal:profile"), data)
    assert resp.status_code == 302
    person.refresh_from_db()
    assert person.email_confidential is True
    assert not person.phone_unlisted and not person.acp_participant


def test_board_form_sets_acp_and_unlisted(people, client):
    owner = people["owner"]
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    data = {
        "full_name": owner.full_name,
        "email": owner.email,
        "preferred_language": "en",
        "phone_unlisted": "on",
        "acp_participant": "on",
        "acp_address": "PO Box 257",
    }
    resp = client.post(reverse("registry:person_edit", args=[owner.pk]), data)
    assert resp.status_code == 302
    owner.refresh_from_db()
    assert owner.phone_unlisted and owner.acp_participant and owner.acp_address == "PO Box 257"
    event = AuditEvent.objects.filter(
        object_type="registry.person", object_id=str(owner.pk)
    ).latest("pk")
    assert {"phone_unlisted", "acp_participant", "acp_address"} <= set(event.changes)
