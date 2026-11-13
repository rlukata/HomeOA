"""Invitations held until residents are switched on."""

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import invitations
from homeoa.access.models import Invitation
from homeoa.access.testing import close_residents, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.notify.models import Notification
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Person

User = get_user_model()
BOARD = "treasurer@cedarrow.example"


@pytest.fixture
def pilot(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


@pytest.fixture
def person(pilot):
    """An owner with an email, consent and no login yet."""
    p = Person.objects.create(full_name="Ivy Invitee", email="ivy@example.com")
    NoticePreference.objects.create(person=p, delivery="email", e_delivery_consent=True)
    return p


def test_draft_and_send_refused_while_off(pilot, person):
    inv = invitations.draft_invitation(person)
    assert inv.status == Invitation.Status.DRAFT
    assert invitations.draft_invitation(person) == inv  # one per person
    with pytest.raises(invitations.InvitationsHeld):
        invitations.send_invitation(inv)
    inv.refresh_from_db()
    assert inv.status == Invitation.Status.DRAFT
    assert not Notification.objects.filter(type_key=invitations.TYPE_KEY).exists()


def test_send_when_on_queues_notify_and_links_user(pilot, person):
    open_residents(pilot)
    inv = invitations.draft_invitation(person)
    notes = invitations.send_invitation(inv, login_url="https://x.example/login/")
    assert len(notes) == 1
    note = notes[0]
    assert note.type_key == invitations.TYPE_KEY
    assert "https://x.example/login/?email=" in note.body
    assert list(note.deliveries.values_list("channel", flat=True)) == ["email"]
    person.refresh_from_db()
    assert person.user is not None and person.user.email == person.email
    inv.refresh_from_db()
    assert inv.status == Invitation.Status.SENT and inv.sent_at
    assert AuditEvent.objects.filter(action="invitation.sent").count() == 1
    with pytest.raises(invitations.InvitationError):
        invitations.send_invitation(inv)


def test_draft_requires_email(pilot):
    p = Person.objects.create(full_name="No Mail")
    with pytest.raises(invitations.InvitationError):
        invitations.draft_invitation(p)


def _owned_person(pilot):
    p = Person.objects.get(user__email="ownera@cedarrow.example")
    return p, reg.units_owned_by(p)[0]


def test_unit_page_invite_disabled_and_send_view_refused_while_off(client, pilot):
    p, unit = _owned_person(pilot)
    client.force_login(User.objects.get(email=BOARD))
    html = client.get(f"/units/{unit.number}/").content.decode()
    assert 'data-testid="invites-held"' in html
    assert "Invite drafted residents</button>" in html
    assert "disabled" in html.split("Invite drafted residents")[0].rsplit("<button", 1)[1]
    resp = client.post(f"/units/{unit.number}/invitations/draft/{p.pk}/")
    assert resp.status_code == 302
    assert Invitation.objects.filter(person=p).exists()
    assert client.post(f"/units/{unit.number}/invitations/send/").status_code == 403
    assert Invitation.objects.get(person=p).status == Invitation.Status.DRAFT


def test_send_view_when_on(client, pilot):
    p, unit = _owned_person(pilot)
    NoticePreference.objects.create(person=p, delivery="email", e_delivery_consent=True)
    open_residents(pilot)
    invitations.draft_invitation(p)
    client.force_login(User.objects.get(email=BOARD))
    html = client.get(f"/units/{unit.number}/").content.decode()
    assert 'data-testid="invites-held"' not in html
    resp = client.post(f"/units/{unit.number}/invitations/send/")
    assert resp.status_code == 302
    assert Invitation.objects.get(person=p).status == Invitation.Status.SENT
    assert Notification.objects.filter(type_key=invitations.TYPE_KEY, recipient=p).exists()


def test_login_page_prefills_email(client, db):
    html = client.get("/login/?email=a%40b.example").content.decode()
    assert 'value="a@b.example"' in html


# --- resend, expiry, revoke ---------------------------------------------------


def test_expiry_default_setting_and_flag(pilot, person):
    from datetime import timedelta

    from django.utils import timezone

    assert invitations.expiry_days(pilot) == invitations.DEFAULT_EXPIRY_DAYS == 30
    open_residents(pilot)
    inv = invitations.draft_invitation(person)
    assert inv.expires_at is None
    invitations.send_invitation(inv)
    assert inv.expires_at == inv.sent_at + timedelta(days=30) and not inv.is_expired
    invitations.set_expiry_days(pilot, 7)
    assert AuditEvent.objects.filter(action="association.invitation_expiry").exists()
    inv.refresh_from_db()
    inv.sent_at = timezone.now() - timedelta(days=8)
    assert inv.is_expired
    pilot.settings["invitation_expiry_days"] = "junk"
    assert invitations.expiry_days(pilot) == 30
    with pytest.raises(invitations.InvitationError):
        invitations.set_expiry_days(pilot, 0)


def test_resend_held_while_off_then_sends(pilot, person):
    open_residents(pilot)
    inv = invitations.draft_invitation(person)
    with pytest.raises(invitations.InvitationError):
        invitations.resend_invitation(inv)  # not sent yet
    invitations.send_invitation(inv)
    close_residents(pilot)
    inv = Invitation.objects.get(pk=inv.pk)  # fresh association settings
    with pytest.raises(invitations.InvitationsHeld):
        invitations.resend_invitation(inv)
    open_residents(pilot)
    inv = Invitation.objects.get(pk=inv.pk)
    notes = invitations.resend_invitation(inv, login_url="https://x.example/login/")
    assert len(notes) == 1 and "token" not in notes[0].body
    assert "https://x.example/login/?email=" in notes[0].body
    inv.refresh_from_db()
    assert inv.send_count == 2
    assert AuditEvent.objects.filter(action="invitation.resent").count() == 1


def test_revoke_and_redraft(pilot, person):
    inv = invitations.draft_invitation(person)
    invitations.revoke_invitation(inv)  # allowed while residents are off
    inv.refresh_from_db()
    assert inv.status == Invitation.Status.REVOKED and inv.revoked_at
    assert AuditEvent.objects.filter(action="invitation.revoked").count() == 1
    with pytest.raises(invitations.InvitationError):
        invitations.revoke_invitation(inv)
    open_residents(pilot)
    with pytest.raises(invitations.InvitationError):
        invitations.send_invitation(inv)
    with pytest.raises(invitations.InvitationError):
        invitations.resend_invitation(inv)
    again = invitations.draft_invitation(person)
    assert again.pk == inv.pk and again.status == Invitation.Status.DRAFT
    assert again.revoked_at is None
    assert AuditEvent.objects.filter(action="invitation.redrafted").count() == 1


def test_lifecycle_views(client, pilot):
    p, unit = _owned_person(pilot)
    NoticePreference.objects.create(person=p, delivery="email", e_delivery_consent=True)
    inv = invitations.draft_invitation(p)
    client.force_login(User.objects.get(email=BOARD))
    base = f"/units/{unit.number}/invitations/{inv.pk}"
    # resend refused while residents are off
    assert client.post(f"{base}/resend/").status_code == 403
    open_residents(pilot)
    invitations.send_invitation(Invitation.objects.get(pk=inv.pk))
    html = client.get(f"/units/{unit.number}/").content.decode()
    assert "Resend invitation to" in html and "Revoke invitation for" in html
    assert client.post(f"{base}/resend/").status_code == 302
    inv.refresh_from_db()
    assert inv.send_count == 2
    assert client.post(f"{base}/revoke/").status_code == 302
    inv.refresh_from_db()
    assert inv.status == Invitation.Status.REVOKED
    # wrong unit -> 404
    other = reg.units_owned_by(p)[0].__class__.objects.exclude(pk=unit.pk).first()
    assert client.post(f"/units/{other.number}/invitations/{inv.pk}/revoke/").status_code == 404


def test_lifecycle_views_need_units_manage(client, pilot):
    p, unit = _owned_person(pilot)
    inv = invitations.draft_invitation(p)
    client.force_login(User.objects.get(email="ownera@cedarrow.example"))
    base = f"/units/{unit.number}/invitations/{inv.pk}"
    # an owner without units.manage is turned away (redirect while residents are off)
    assert client.post(f"{base}/revoke/").status_code in (302, 403)
    assert client.post(f"{base}/resend/").status_code in (302, 403)
    inv.refresh_from_db()
    assert inv.status == Invitation.Status.DRAFT
