import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.vendors import services
from homeoa.vendors.models import Vendor, VendorContact, mask_account, mask_tin

TIN = "987654321"
START = dt.date(2020, 1, 1)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def vendor(assoc):
    v = services.save_vendor(
        Vendor(
            name="Greenway Landscaping",
            category="Grounds",
            tin=TIN,
            tin_type="ein",
            account_number="ACCT-55501234",
            payee_name="Greenway LLC",
            remit_address_line1="PO Box 12",
            remit_city="Olympia",
            remit_region="WA",
            remit_postal_code="98501",
        )
    )
    VendorContact.objects.create(vendor=v, name="Luis Ortega", role="Owner", is_primary=True)
    return v


def _user(email, assoc, **kw):
    user = get_user_model().objects.create_user(email=email)
    grant(user, association=assoc, start=START, **kw)
    return user


@pytest.fixture
def treasurer(assoc):
    return _user("t@example.org", assoc, title="treasurer")


@pytest.fixture
def secretary(assoc):
    return _user("s@example.org", assoc, title="secretary")


# --- models and masking -----------------------------------------------------------------


def test_masks():
    assert mask_tin("123-45-6789", "ssn") == "***-**-6789"
    assert mask_tin("12-3456789", "ein") == "**-***6789"
    assert mask_tin("", "ein") == ""
    assert mask_account("ACCT-55501234") == "••••1234"


def test_tin_and_account_encrypted_at_rest(vendor):
    with connection.cursor() as cur:
        cur.execute("SELECT tin, account_number FROM vendors_vendor WHERE id = %s", [vendor.pk])
        raw_tin, raw_acct = cur.fetchone()
    assert TIN not in raw_tin and "6789" not in raw_tin
    assert "55501234" not in raw_acct
    vendor.refresh_from_db()
    assert vendor.tin == TIN
    assert vendor.tin_masked == "**-***4321"


def test_slug_unique_and_reserved(assoc):
    a = services.save_vendor(Vendor(name="Acme"))
    b = services.save_vendor(Vendor(name="Acme"))
    c = services.save_vendor(Vendor(name="Add"))
    assert (a.slug, b.slug, c.slug) == ("acme", "acme-2", "add-vendor")
    d = services.save_vendor(Vendor(name="Insurance"))
    assert d.slug == "insurance-vendor"


def test_vendor_hub_tabs_all_resolve():
    """Every Vendors hub tab links to a real page, not the vendor-detail slug route."""
    from django.urls import resolve

    from homeoa.ui.nav import get_hub

    for tab in get_hub("vendors").subtabs:
        url = f"/vendors/{'' if tab.slug == 'all' else tab.slug + '/'}"
        assert resolve(url).url_name != "detail", url


def test_create_and_edit_audited_without_secrets(vendor):
    events = AuditEvent.objects.filter(object_type="vendors.vendor", object_id=str(vendor.pk))
    assert events.count() == 1
    vendor.tin = "111223333"
    vendor.save()
    last = events.order_by("-pk").first()
    assert last.changes["tin"] == "changed"
    assert "111223333" not in str(last.changes)
    assert AuditEvent.objects.filter(object_type="vendors.vendorcontact").exists()


def test_search_and_status(assoc, vendor):
    services.save_vendor(Vendor(name="Old Plumber", status=Vendor.Status.INACTIVE))
    qs = Vendor.objects.all()
    assert list(services.search(qs, "green")) == [vendor]
    assert list(services.search(qs, "luis")) == [vendor]
    assert [v.name for v in services.search(qs, "", "inactive")] == ["Old Plumber"]
    assert services.search(qs).count() == 2


def test_scoped_to_association(vendor):
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert Vendor.objects.count() == 0


# --- permissions ------------------------------------------------------------------------


def test_default_permissions(assoc, treasurer, secretary):
    """Every board member edits plain details and proposes payment changes; nobody changes
    payment details on their own."""
    president = _user("p@example.org", assoc, title="president")
    for member in (secretary, treasurer, president):
        assert has_perm(member, perms.VENDORS_READ, association=assoc)
        assert has_perm(member, perms.VENDORS_MANAGE, association=assoc)
        assert has_perm(member, perms.VENDORS_PROPOSE, association=assoc)


def test_reveal_needs_finance_manage_and_is_audited(vendor, treasurer, secretary):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        services.reveal_tin(vendor, secretary)
    assert services.reveal_tin(vendor, treasurer) == "98-7654321"
    event = AuditEvent.objects.get(action="vendors.tin_revealed")
    assert event.actor_id == treasurer.pk
    assert TIN not in str(event.changes) and "7654321" not in event.object_repr


# --- views ------------------------------------------------------------------------------


def test_list_filter_and_testids(client, assoc, vendor, secretary):
    services.save_vendor(Vendor(name="Old Plumber", status=Vendor.Status.INACTIVE))
    client.force_login(secretary)
    resp = client.get("/vendors/")
    body = resp.content.decode()
    assert resp.status_code == 200
    assert 'data-testid="vendor-row-greenway-landscaping"' in body
    assert "vendor-row-old-plumber" not in body
    assert "Add vendor" in body  # any board member can propose one
    body = client.get("/vendors/?status=all&q=plumb").content.decode()
    assert "vendor-row-old-plumber" in body and "vendor-row-greenway" not in body
    htmx = client.get("/vendors/?q=green", HTTP_HX_REQUEST="true").content.decode()
    assert htmx.startswith("<tbody") and "greenway" in htmx


def test_detail_never_renders_tin(client, vendor, treasurer, secretary):
    for user in (secretary, treasurer):
        client.force_login(user)
        body = client.get(f"/vendors/{vendor.slug}/").content.decode()
        assert TIN not in body and "98-7654321" not in body
        assert "**-***4321" in body and "••••1234" in body and "55501234" not in body
    assert "reveal-tin" in body  # treasurer
    client.force_login(secretary)
    assert "reveal-tin" not in client.get(f"/vendors/{vendor.slug}/").content.decode()


def test_reveal_view(client, vendor, treasurer, secretary):
    client.force_login(secretary)
    assert client.post(f"/vendors/{vendor.slug}/tin/").status_code == 403
    client.force_login(treasurer)
    assert client.get(f"/vendors/{vendor.slug}/tin/").status_code == 405
    resp = client.post(f"/vendors/{vendor.slug}/tin/")
    assert resp.status_code == 200 and "98-7654321" in resp.content.decode()
    assert resp["Cache-Control"] == "no-store"
    assert AuditEvent.objects.filter(action="vendors.tin_revealed").count() == 1


def test_edit_form_never_prefills_and_blank_keeps(client, vendor, treasurer):
    client.force_login(treasurer)
    form = client.get(f"/vendors/{vendor.slug}/edit/").content.decode()
    assert TIN not in form and "55501234" not in form
    data = {"name": vendor.name, "status": "active", "tin_type": "ein", "tin_input": ""}
    resp = client.post(f"/vendors/{vendor.slug}/edit/", data)
    assert resp.status_code == 302
    vendor.refresh_from_db()
    assert vendor.tin == TIN and vendor.account_number == "ACCT-55501234"
    data["tin_input"] = "12-3"
    assert client.post(f"/vendors/{vendor.slug}/edit/", data).status_code == 200


def test_add_vendor_with_w9(client, assoc, treasurer):
    load_chart_of_accounts(assoc)
    from homeoa.ledger.models import Account

    acct = Account.objects.get(code="5100")
    client.force_login(treasurer)
    pdf = SimpleUploadedFile("w9.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    resp = client.post(
        "/vendors/add/",
        {
            "name": "Brightline Electric",
            "status": "active",
            "tin_type": "ssn",
            "tin_input": "123-45-6789",
            "default_expense_account": acct.pk,
            "is_1099_eligible": "on",
            "account_input": "998877",
            "w9_file": pdf,
        },
    )
    assert resp.status_code == 302
    v = Vendor.objects.get(slug="brightline-electric")
    assert v.tin == "123456789" and v.tin_masked == "***-**-6789"
    assert v.default_expense_account == acct and v.is_1099_eligible
    assert v.w9_file_key.startswith(f"{assoc.pk}/")
    w9 = client.get(f"/vendors/{v.slug}/w9/")
    assert w9.status_code == 200 and w9.content == b"%PDF-1.4 test"


def test_contacts(client, vendor, treasurer):
    client.force_login(treasurer)
    resp = client.post(
        f"/vendors/{vendor.slug}/contacts/add/",
        {"name": "Ana", "email": "ana@example.org", "is_primary": "on"},
    )
    assert resp.status_code == 302
    assert vendor.primary_contact.name == "Ana"
    old = VendorContact.objects.get(name="Luis Ortega")
    assert not old.is_primary
    client.post(f"/vendors/{vendor.slug}/contacts/{old.pk}/delete/")
    assert not VendorContact.objects.filter(pk=old.pk).exists()


def test_maintenance_committee_proposes_but_does_not_edit(client, assoc, vendor):
    member = get_user_model().objects.create_user(email="m@example.org")
    grant(member, "maintenance_committee", association=assoc, start=START)
    client.force_login(member)
    assert client.get("/vendors/add/").status_code == 200
    assert client.get(f"/vendors/{vendor.slug}/edit/").status_code == 200
    assert client.post(f"/vendors/{vendor.slug}/contacts/add/", {"name": "x"}).status_code == 403


def _vendor_form(vendor, **changes):
    data = {
        "name": vendor.name,
        "category": vendor.category,
        "status": vendor.status,
        "tin_type": vendor.tin_type,
        "legal_name": vendor.legal_name,
        "payee_name": vendor.payee_name,
        "remit_address_line1": vendor.remit_address_line1,
        "remit_city": vendor.remit_city,
        "remit_region": vendor.remit_region,
        "remit_postal_code": vendor.remit_postal_code,
    }
    data.update(changes)
    return data


def test_payment_detail_changes_wait_for_board_approval(
    client, assoc, vendor, treasurer, secretary
):
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    client.force_login(treasurer)
    # A plain detail saves at once.
    resp = client.post(f"/vendors/{vendor.slug}/edit/", _vendor_form(vendor, category="Grounds"))
    assert resp.status_code == 302
    vendor.refresh_from_db()
    assert vendor.category == "Grounds" and not ChangeRequest.objects.exists()
    # A new remittance address and account number wait for another board member.
    resp = client.post(
        f"/vendors/{vendor.slug}/edit/",
        _vendor_form(vendor, remit_address_line1="1 Fraud Way", account_input="999000111"),
    )
    change = ChangeRequest.objects.get()
    assert resp.status_code == 302 and resp["Location"] == f"/governance/changes/{change.pk}/"
    vendor.refresh_from_db()
    assert vendor.remit_address_line1 != "1 Fraud Way" and vendor.account_number == "ACCT-55501234"
    assert "999000111" not in str(change.payload) and "999000111" not in str(change.details)
    changes.approve(change, secretary)
    vendor.refresh_from_db()
    assert vendor.remit_address_line1 == "1 Fraud Way" and vendor.account_number == "999000111"


def test_deactivating_a_vendor_needs_approval(client, assoc, vendor, treasurer, secretary):
    from homeoa.changes.models import ChangeRequest

    client.force_login(treasurer)
    client.post(f"/vendors/{vendor.slug}/edit/", _vendor_form(vendor, status="inactive"))
    vendor.refresh_from_db()
    assert vendor.is_active
    assert ChangeRequest.objects.get().summary == "Deactivate vendor Greenway Landscaping"


def test_new_vendor_is_a_change_request(client, assoc, treasurer, secretary):
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    client.force_login(treasurer)
    pdf = SimpleUploadedFile("w9.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    client.post(
        "/vendors/add/",
        {
            "name": "Pest Pros",
            "status": "active",
            "tin_type": "ein",
            "tin_input": "12-3456789",
            "w9_file": pdf,
        },
    )
    assert not Vendor.objects.filter(name="Pest Pros").exists()
    change = ChangeRequest.objects.get()
    assert change.summary == "Add vendor Pest Pros"
    assert "123456789" not in str(change.payload)
    changes.approve(change, secretary)
    v = Vendor.objects.get(name="Pest Pros")
    assert v.tin == "123456789" and v.w9_file_key
    change.refresh_from_db()
    assert change.result_url == f"/vendors/{v.slug}/"


def test_residents_get_403(client, vendor):
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    for url in ("/vendors/", f"/vendors/{vendor.slug}/", f"/vendors/{vendor.slug}/w9/"):
        assert client.get(url).status_code == 403
    assert client.post(f"/vendors/{vendor.slug}/tin/").status_code == 403


def test_demo_seed_adds_vendors(db, settings):
    from homeoa.core import demo

    settings.HOMEOA_ALLOW_DEMO_RESET = True
    association = demo.seed()
    with association_context(association):
        names = set(Vendor.objects.values_list("name", flat=True))
        assert {"Greenway Landscaping", "Evergreen Gutter & Roof"} <= names
        assert "City of Olympia Utilities" in names
        green = Vendor.objects.get(slug="greenway-landscaping")
        assert green.default_expense_account.code == "5100"
        assert green.primary_contact is not None


def test_w9_download_needs_finance_manage_and_is_audited(
    client, assoc, vendor, treasurer, secretary
):
    pdf = SimpleUploadedFile("w9.pdf", b"%PDF-1.4 w9", content_type="application/pdf")
    services.store_w9(vendor, pdf)
    client.force_login(secretary)
    assert client.get(f"/vendors/{vendor.slug}/w9/").status_code == 403
    assert "vendor-w9" not in client.get(f"/vendors/{vendor.slug}/").content.decode()
    assert not AuditEvent.objects.filter(action="vendors.w9_downloaded").exists()
    client.force_login(treasurer)
    assert "vendor-w9" in client.get(f"/vendors/{vendor.slug}/").content.decode()
    assert client.get(f"/vendors/{vendor.slug}/w9/").status_code == 200
    event = AuditEvent.objects.get(action="vendors.w9_downloaded")
    assert event.actor_id == treasurer.pk
