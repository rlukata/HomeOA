"""In-app notifications page and header count."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant, open_residents
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.notify.models import Delivery, Notification
from homeoa.registry.models import Person

User = get_user_model()
TREASURER = "treasurer@cedarrow.example"
SECRETARY = "secretary@cedarrow.example"
OWNER = "ownerc@cedarrow.example"


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    return demo.seed()


def _person(a, email):
    user = User.objects.get(email=email)
    with association_context(a):
        person = Person.objects.filter(user=user).first()
        if person is None:
            person = Person.objects.create(association=a, user=user, full_name=email)
    return person


def _note(a, email, subject, *, status=Delivery.Status.SENT, channel="inapp"):
    person = _person(a, email)
    with association_context(a):
        n = Notification.objects.create(
            association=a,
            type_key="test.alert",
            recipient=person,
            subject=subject,
            body="Body of " + subject,
            idempotency_key=f"test|{person.pk}|{subject}",
            as_of=dt.date(2026, 10, 7),
        )
        Delivery.objects.create(association=a, notification=n, channel=channel, status=status)
    return n


def _login(client, email):
    client.force_login(User.objects.get(email=email))


def test_board_sees_only_own_inapp_notifications_newest_first(client, assoc):
    first = _note(assoc, TREASURER, "Older alert")
    second = _note(assoc, TREASURER, "Newer alert")
    other = _note(assoc, SECRETARY, "Secretary alert")
    email_only = _note(assoc, TREASURER, "Email only", channel="email")
    held = _note(assoc, TREASURER, "Held preview", status=Delivery.Status.PREVIEW)
    _login(client, TREASURER)
    resp = client.get("/notifications/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert f'data-testid="notif-row-{first.pk}"' in html
    assert html.index(f"notif-row-{second.pk}") < html.index(f"notif-row-{first.pk}")
    for hidden in (other, email_only, held):
        assert f"notif-row-{hidden.pk}" not in html
    assert 'data-testid="notif-count" aria-hidden="true"' in html
    assert ">2</span>" in html


def test_mark_read_single_and_all(client, assoc):
    a = _note(assoc, TREASURER, "One")
    b = _note(assoc, TREASURER, "Two")
    other = _note(assoc, SECRETARY, "Not mine")
    _login(client, TREASURER)
    assert client.post(f"/notifications/{a.pk}/read/").status_code == 302
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.read_at is not None and b.read_at is None
    assert client.post(f"/notifications/{other.pk}/read/").status_code == 404
    client.post("/notifications/read-all/")
    b.refresh_from_db()
    other.refresh_from_db()
    assert b.read_at is not None and other.read_at is None
    html = client.get("/notifications/").content.decode()
    assert 'data-testid="notif-count"' not in html
    assert 'data-testid="notif-bell"' in html


def test_get_not_allowed_on_mark_read(client, assoc):
    n = _note(assoc, TREASURER, "One")
    _login(client, TREASURER)
    assert client.get(f"/notifications/{n.pk}/read/").status_code == 405


def test_resident_gate(client, assoc):
    n = _note(assoc, OWNER, "Owner alert")
    _login(client, OWNER)
    # The resident-access middleware sends locked residents to /me/ (the "not open yet" page).
    resp = client.get("/notifications/")
    assert resp.status_code == 302 and resp["Location"] == "/me/"
    assert client.post(f"/notifications/{n.pk}/read/").status_code == 302
    html = client.get("/me/").content.decode()
    assert 'data-testid="notif-bell"' not in html
    n.refresh_from_db()
    assert n.read_at is None

    with association_context(assoc):
        open_residents(assoc)
    resp = client.get("/notifications/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert f'data-testid="notif-row-{n.pk}"' in html
    assert 'class="me"' in html  # resident shell
    assert 'data-testid="notif-count"' in html


def test_a_system_admin_off_the_board_sees_their_notifications_before_go_live(client, assoc):
    user = User.objects.create_user("admin@cedarrow.example", full_name="Ada Admin")
    grant(user, "sys_admin", association=assoc)
    n = _note(assoc, "admin@cedarrow.example", "Law check")
    _login(client, "admin@cedarrow.example")
    resp = client.get("/notifications/")
    assert resp.status_code == 200
    assert f'data-testid="notif-row-{n.pk}"' in resp.content.decode()


def test_anonymous_redirects_to_login(client, assoc):
    resp = client.get("/notifications/")
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


def test_counts_only_current_association(client, assoc):
    from homeoa.core.models import Association

    mine = _note(assoc, TREASURER, "Here")
    other_assoc = Association.objects.create(name="Other HOA", short_name="OH")
    elsewhere = _note(other_assoc, TREASURER, "Elsewhere")
    assert elsewhere.recipient.association_id == other_assoc.pk
    _login(client, TREASURER)
    html = client.get("/notifications/").content.decode()
    assert f'data-testid="notif-row-{mine.pk}"' in html
    assert f"notif-row-{elsewhere.pk}" not in html
    assert '<span class="notif-count" data-testid="notif-count" aria-hidden="true">1</span>' in html
    assert client.post(f"/notifications/{elsewhere.pk}/read/").status_code == 404
