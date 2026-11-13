"""Payment history, statements tab, profile, household, preferences, consent."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils.html import escape

from homeoa.access import services as access
from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.notify import types as ntypes
from homeoa.notify.models import Preference
from homeoa.payments.services import record_receipt, void_receipt
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Occupancy, Person, Unit
from homeoa.statements import owner_statements as svc
from homeoa.statements.statement_models import Statement

User = get_user_model()
OCT = dt.date(2026, 10, 1)
C_EMAIL = "ownerc@cedarrow.example"
A_EMAIL = "ownera@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        if not Charge.objects.filter(period=OCT).exists():
            generate_assessments(AssessmentSchedule.objects.get(), OCT, source="test")
        open_residents(a)
        yield a


def _person(email):
    return Person.objects.get(user__email=email)


def _unit_of(email):
    return reg.units_owned_by(_person(email))[0]


@pytest.fixture
def owner_c(client, pilot):
    client.force_login(User.objects.get(email=C_EMAIL))
    return client


def test_history_shows_receipts_with_status(owner_c, pilot):
    unit = _unit_of(C_EMAIL)
    ok = record_receipt(
        unit=unit, amount=Decimal("10.00"), received_date=OCT, method="zelle", reference="r1"
    )
    bad = record_receipt(
        unit=unit, amount=Decimal("20.00"), received_date=OCT, method="ach", reference="r2"
    )
    void_receipt(bad, reason="returned")
    html = owner_c.get("/me/payments/").content.decode()
    assert html.count('data-testid="payment-status"') >= 2
    assert "Returned / void" in html and "Received" in html
    assert 'data-testid="ledger"' in html
    assert ok.reference in html


def test_history_hides_previous_owner(client, pilot):
    """A buyer does not see the seller's receipts."""
    unit = _unit_of(C_EMAIL)
    record_receipt(
        unit=unit, amount=Decimal("15.00"), received_date=OCT, method="check", reference="SELLER"
    )
    buyer_user = User.objects.create_user(email="buyer@example.com", password="x-long-pass-1")
    buyer = Person.objects.create(full_name="New Buyer", user=buyer_user)
    reg.transfer_ownership(unit, [buyer], dt.date(2026, 10, 5))
    access.clear_cache(buyer_user)
    client.force_login(buyer_user)
    html = client.get("/me/payments/").content.decode()
    assert "SELLER" not in html


def test_statements_own_only(owner_c, pilot):
    run = svc.generate(pilot, OCT)
    mine = run.statements.get(unit=_unit_of(C_EMAIL))
    other = run.statements.exclude(unit=_unit_of(C_EMAIL)).first()
    run.statements.update(status=Statement.Status.SENT)
    html = owner_c.get("/me/statements/").content.decode()
    assert f"/me/statements/{mine.pk}/pdf/" in html
    assert f"/me/statements/{other.pk}/pdf/" not in html
    resp = owner_c.get(f"/me/statements/{mine.pk}/pdf/")
    assert resp.status_code == 200 and resp["Content-Type"] == "application/pdf"
    assert owner_c.get(f"/me/statements/{other.pk}/pdf/").status_code == 404


def test_statements_show_autopay_line(owner_c, pilot):
    run = svc.generate(pilot, OCT)
    run.statements.update(status=Statement.Status.SENT)
    mine = run.statements.get(unit=_unit_of(C_EMAIL))
    mine.autopay_debit_on = dt.date(2026, 10, 5)
    mine.autopay_amount = Decimal("412.50")
    mine.autopay_fixed = True
    mine.save()
    html = owner_c.get("/me/statements/").content.decode()
    expected = svc.statement_autopay_text(mine)
    assert expected
    assert f'data-testid="statement-autopay-{mine.pk}"' in html
    assert escape(expected) in html


def test_statements_no_autopay_line_without_debit(owner_c, pilot):
    run = svc.generate(pilot, OCT)
    run.statements.update(status=Statement.Status.SENT, autopay_debit_on=None)
    html = owner_c.get("/me/statements/").content.decode()
    assert "statement-autopay-" not in html


def test_preview_statement_hidden(owner_c, pilot):
    run = svc.generate(pilot, OCT)
    mine = run.statements.get(unit=_unit_of(C_EMAIL))
    Statement.objects.filter(pk=mine.pk).update(status=Statement.Status.PREVIEW)
    html = owner_c.get("/me/statements/").content.decode()
    assert f"/me/statements/{mine.pk}/pdf/" not in html
    assert owner_c.get(f"/me/statements/{mine.pk}/pdf/").status_code == 404


def test_tenant_cannot_open_statements(client, pilot):
    run = svc.generate(pilot, OCT)
    st = run.statements.first()
    client.force_login(User.objects.get(email="chris.doyle@cedarrow.example"))
    assert client.get(f"/me/statements/{st.pk}/pdf/").status_code == 403


def test_profile_edit_contact_is_audited(owner_c, pilot):
    person = _person(C_EMAIL)
    data = {f: getattr(person, f) for f in reg.CONTACT_FIELDS}
    data["phone"] = "206-555-0199"
    resp = owner_c.post("/me/profile/", data)
    assert resp.status_code == 302
    person.refresh_from_db()
    assert person.phone == "206-555-0199"
    assert AuditEvent.objects.filter(object_type="registry.person", object_id=person.pk).exists()


def test_household_add_and_remove(owner_c, pilot):
    unit = _unit_of(C_EMAIL)
    resp = owner_c.post("/me/profile/household/", {"full_name": "Sam Kid", "email": ""})
    assert resp.status_code == 302
    occ = Occupancy.objects.get(unit=unit, person__full_name="Sam Kid")
    assert occ.kind == Occupancy.Kind.RESIDENT and occ.end is None
    assert "Sam Kid" in owner_c.get("/me/profile/").content.decode()
    owner_c.post(f"/me/profile/household/{occ.pk}/end/")
    occ.refresh_from_db()
    assert occ.end == dt.date(2026, 10, 7)


def test_household_of_other_unit_refused(owner_c, pilot):
    other = Unit.objects.exclude(pk=_unit_of(C_EMAIL).pk).first()
    occ = reg.add_occupancy(
        other,
        Person.objects.create(full_name="Not Yours"),
        dt.date(2026, 1, 1),
        kind=Occupancy.Kind.RESIDENT,
    )
    assert owner_c.post(f"/me/profile/household/{occ.pk}/end/").status_code == 404


def test_tenant_has_no_household_section(client, pilot):
    client.force_login(User.objects.get(email="chris.doyle@cedarrow.example"))
    html = client.get("/me/profile/").content.decode()
    assert 'data-testid="household"' not in html
    assert client.post("/me/profile/household/", {"full_name": "X"}).status_code == 403


def test_consent_given_and_withdrawn(owner_c, pilot):
    person = _person(C_EMAIL)
    assert owner_c.post("/me/profile/consent/", {"action": "give"}).status_code == 302
    before = reg.current_notice_preference(person)
    # No box ticked: nothing recorded.
    assert before is None or before.source != NoticePreference.Source.PORTAL
    from homeoa.registry import consent

    owner_c.post(
        "/me/profile/consent/",
        {"action": "give", "agree": "yes", "email": C_EMAIL, "version": consent.VERSION},
    )
    pref = reg.current_notice_preference(person)
    assert pref.e_delivery_consent and pref.source == NoticePreference.Source.PORTAL
    assert pref.recorded_at is not None
    html = owner_c.get("/me/profile/").content.decode()
    assert 'data-testid="consent-at"' in html
    assert "Draft wording" in html
    owner_c.post("/me/profile/consent/", {"action": "withdraw"})
    latest = reg.current_notice_preference(person)
    assert latest.pk != pref.pk
    assert not latest.e_delivery_consent and latest.delivery == NoticePreference.Delivery.POSTAL
    assert latest.source == NoticePreference.Source.PORTAL


def test_preferences_saved(owner_c, pilot):
    key = "test.m207.optional"
    ntypes.register(
        ntypes.NotificationType(key=key, label="Test optional", default_channels=("inapp",))
    )
    try:
        person = _person(C_EMAIL)
        html = owner_c.get("/me/profile/").content.decode()
        assert f'name="{key}:inapp" checked' in html
        owner_c.post("/me/profile/notifications/", {f"{key}:email": "on"})
        prefs = {
            p.channel: p.enabled for p in Preference.objects.filter(person=person, type_key=key)
        }
        assert prefs == {"inapp": False, "email": True}
    finally:
        ntypes.unregister(key)


def test_more_links_to_profile(owner_c, pilot):
    assert "/me/profile/" in owner_c.get("/me/more/").content.decode()
