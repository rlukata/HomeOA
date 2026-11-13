import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.vendors import alerts, services
from homeoa.vendors.models import (
    Contract,
    DocumentRequest,
    Vendor,
    VendorAlert,
    VendorContact,
    VendorDocument,
)

EXPIRY = dt.date(2026, 12, 31)
START = dt.date(2020, 1, 1)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def vendor(assoc):
    v = services.save_vendor(Vendor(name="Greenway Landscaping"))
    VendorContact.objects.create(
        vendor=v, name="Luis Ortega", email="luis@greenway.example", is_primary=True
    )
    return v


@pytest.fixture
def coi(vendor):
    return VendorDocument.objects.create(
        vendor=vendor, kind="coi", issuer="Acme Mutual", expiry_date=EXPIRY
    )


@pytest.fixture
def treasurer(assoc):
    user = get_user_model().objects.create_user(email="t@example.org")
    grant(user, association=assoc, start=START, title="treasurer")
    return user


def _alerts(assoc):
    return list(VendorAlert.objects.filter(resolved_on__isnull=True))


def test_coi_alert_exactly_at_window(assoc, coi):
    window_start = EXPIRY - dt.timedelta(days=30)
    assert alerts.run(assoc, window_start - dt.timedelta(days=1)) == []
    [alert] = alerts.run(assoc, window_start)
    assert alert.document == coi and alert.due == EXPIRY
    [item] = alerts.todo_items(assoc)
    assert item["title"] == "Request new insurance certificate: Greenway Landscaping"
    assert item["action"] == "Send request"


def test_window_setting(assoc, coi, settings):
    settings.HOMEOA_VENDOR_DOC_WARNING_DAYS = 10
    assert alerts.run(assoc, EXPIRY - dt.timedelta(days=11)) == []
    assert len(alerts.run(assoc, EXPIRY - dt.timedelta(days=10))) == 1


def test_job_idempotent(assoc, coi):
    day = EXPIRY - dt.timedelta(days=5)
    alerts.run(assoc, day)
    assert alerts.run(assoc, day) == []
    assert alerts.run(assoc, day + dt.timedelta(days=1)) == []
    assert VendorAlert.objects.count() == 1


def test_renewed_certificate_resolves_alert(assoc, vendor, coi):
    alerts.run(assoc, EXPIRY - dt.timedelta(days=5))
    VendorDocument.objects.create(vendor=vendor, kind="coi", expiry_date=EXPIRY.replace(2027))
    alerts.run(assoc, EXPIRY - dt.timedelta(days=4))
    assert _alerts(assoc) == []
    assert VendorDocument.objects.count() == 2  # old one kept


def test_expired_vendor_ids(assoc, vendor, coi):
    assert alerts.expired_vendor_ids(assoc, EXPIRY) == set()
    assert alerts.expired_vendor_ids(assoc, EXPIRY + dt.timedelta(days=1)) == {vendor.pk}


def test_contract_auto_renew_notice_date(assoc, vendor):
    c = Contract.objects.create(
        vendor=vendor,
        title="Landscaping 2026",
        start_date=dt.date(2026, 1, 1),
        end_date=EXPIRY,
        auto_renew=True,
        notice_days=60,
        amount=Decimal("4800.00"),
    )
    notice = EXPIRY - dt.timedelta(days=60)
    assert c.notice_date == notice
    assert alerts.run(assoc, notice - dt.timedelta(days=1)) == []
    [alert] = alerts.run(assoc, notice)
    assert alert.contract == c and alert.due == notice
    [item] = alerts.todo_items(assoc)
    assert item["title"] == "Decide on renewal: Landscaping 2026"
    alerts.resolve(alert, notice)
    assert alerts.todo_items(assoc) == []
    assert alerts.run(assoc, notice + dt.timedelta(days=1)) == []


def test_contract_without_auto_renew_has_no_alert(assoc, vendor):
    Contract.objects.create(
        vendor=vendor, title="One-off", start_date=START, end_date=EXPIRY, notice_days=30
    )
    assert alerts.run(assoc, EXPIRY - dt.timedelta(days=10)) == []


def test_todo_registered(assoc, coi):
    alerts.run(assoc, EXPIRY)
    assert any(i.id.startswith("vendor-alert-") for i in todos.collect(assoc))


def test_send_request_creates_one_record_and_email(assoc, coi, treasurer):
    req = alerts.send_document_request(coi, treasurer)
    assert req.sent and req.email == "luis@greenway.example"
    assert DocumentRequest.objects.count() == 1
    [msg] = mail.outbox
    assert msg.to == ["luis@greenway.example"]
    assert "insurance certificate" in msg.subject and "2026-12-31" in msg.body
    assert AuditEvent.objects.filter(action="vendors.document_requested").count() == 1
    alerts.run(assoc, EXPIRY)
    [item] = alerts.todo_items(assoc)
    assert "post_url" not in item and "requested" in item["sub"]


def test_send_request_needs_email(assoc, vendor, coi):
    vendor.contacts.update(email="")
    with pytest.raises(ValueError):
        alerts.send_document_request(coi)


def test_views(client, assoc, vendor, treasurer):
    client.force_login(treasurer)
    resp = client.post(
        f"/vendors/{vendor.slug}/certificates/add/",
        {
            "kind": "coi",
            "issuer": "Acme",
            "expiry_date": "2026-01-01",
            "upload": SimpleUploadedFile("coi.pdf", b"%PDF-1", content_type="application/pdf"),
        },
    )
    assert resp.status_code == 302
    doc = VendorDocument.objects.get()
    assert doc.file_key
    assert client.get(f"/vendors/certificates/{doc.pk}/file/").content == b"%PDF-1"
    resp = client.post(
        f"/vendors/{vendor.slug}/contracts/add/",
        {
            "title": "Snow removal",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "auto_renew": "on",
            "notice_days": "30",
            "amount": "1200.00",
        },
    )
    assert resp.status_code == 302
    body = client.get(f"/vendors/{vendor.slug}/").content.decode()
    assert "Snow removal" in body and "$1,200.00" in body
    assert client.post(f"/vendors/certificates/{doc.pk}/request/").status_code == 302
    assert len(mail.outbox) == 1
    listing = client.get("/vendors/").content.decode()
    assert f"vendor-expired-{vendor.slug}" in listing


def test_views_need_manage(client, assoc, vendor, coi):
    """The Maintenance committee reads vendors but does not manage their paperwork."""
    user = get_user_model().objects.create_user(email="m@example.org")
    grant(user, "maintenance_committee", association=assoc, start=START)
    client.force_login(user)
    assert client.post(f"/vendors/certificates/{coi.pk}/request/").status_code == 403
    assert client.get(f"/vendors/{vendor.slug}/certificates/add/").status_code == 403
