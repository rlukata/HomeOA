"""Owners' welcome packet: the board's message and marked documents on a Welcome page."""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access import invitations
from homeoa.access.testing import open_residents
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents import services as docs
from homeoa.documents import welcome
from homeoa.documents.models import Document, Visibility
from homeoa.plugins import registry as plugin_registry
from homeoa.registry import services as reg
from homeoa.registry.models import Person, Unit

User = get_user_model()
OWNER_EMAIL = "ownerc@cedarrow.example"
TENANT_EMAIL = "chris.doyle@cedarrow.example"
SECRETARY_EMAIL = "secretary@cedarrow.example"
TREASURER_EMAIL = "treasurer@cedarrow.example"


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


def _doc(title):
    return Document.objects.get(title=title)


def test_demo_packet_has_the_governing_documents_and_a_message(pilot):
    titles = {d.title for d in welcome.documents(pilot)}
    assert "Bylaws" in titles and "Budget 2026" in titles
    assert "Executive session notes · Sep 17, 2026" not in titles
    assert welcome.packet(pilot).message.startswith(f"Welcome to {pilot.name}!")


def test_board_only_documents_stay_out_of_the_packet(pilot):
    notes = _doc("Executive session notes · Sep 17, 2026")
    with pytest.raises(docs.UploadRejected):
        welcome.set_in_packet(notes, True)
    bylaws = _doc("Bylaws")
    docs.set_visibility(bylaws, Visibility.BOARD)
    bylaws.refresh_from_db()
    assert not bylaws.in_welcome_packet
    assert "Bylaws" not in {d.title for d in welcome.documents(pilot)}


def test_owner_reads_packet_and_home_card_goes_away(pilot, client):
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    home = client.get(reverse("portal:home")).content.decode()
    assert 'data-testid="welcome-prompt"' in home
    assert (
        reverse("documents:portal_welcome") in client.get(reverse("portal:more")).content.decode()
    )
    page = client.get(reverse("documents:portal_welcome"))
    assert page.status_code == 200
    body = page.content.decode()
    assert f"Welcome to {pilot.name}!" in body and "Bylaws" in body
    assert 'data-testid="contact-board"' in body
    home = client.get(reverse("portal:home")).content.decode()
    assert 'data-testid="welcome-prompt"' not in home


def test_tenant_gets_no_packet(pilot, client):
    client.force_login(User.objects.get(email=TENANT_EMAIL))
    assert "more-welcome" not in client.get(reverse("portal:more")).content.decode()
    assert client.get(reverse("documents:portal_welcome")).status_code == 403


def test_secretary_writes_the_message_and_marks_documents(pilot, client):
    client.force_login(User.objects.get(email=SECRETARY_EMAIL))
    url = reverse("documents:welcome")
    assert client.get(url).status_code == 200
    client.post(url, {"message": "Hello neighbors"})
    assert welcome.packet(pilot).message == "Hello neighbors"
    insurance = _doc("Master insurance certificate 2026")
    vis = reverse("documents:visibility", args=[insurance.public_id])
    client.post(vis, {"visibility": Visibility.OWNERS, "in_welcome_packet": "on"})
    insurance.refresh_from_db()
    assert insurance.in_welcome_packet
    client.post(vis, {"visibility": Visibility.BOARD, "in_welcome_packet": "on"})
    insurance.refresh_from_db()
    assert insurance.visibility == Visibility.OWNERS  # refused as a whole


def test_other_board_members_read_but_do_not_change_the_message(pilot, client):
    client.force_login(User.objects.get(email=TREASURER_EMAIL))
    url = reverse("documents:welcome")
    assert client.get(url).status_code == 200
    client.post(url, {"message": "Changed"})
    assert welcome.packet(pilot).message.startswith(f"Welcome to {pilot.name}!")


def test_invitation_to_an_owner_mentions_the_packet(pilot):
    owner = reg.current_owners(Unit.objects.get(number="B"))[0]
    owner.email = owner.email or "ownerb@example.test"
    owner.save()
    note = invitations.send_invitation(invitations.draft_invitation(owner))[0]
    assert "welcome packet" in note.body
    outsider = Person.objects.create(full_name="Ivy Invitee", email="ivy@example.com")
    note = invitations.send_invitation(invitations.draft_invitation(outsider))[0]
    assert "welcome packet" not in note.body
