"""The residents switch, the /me/ shell and the roles-page error display."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail

from homeoa.access import services
from homeoa.access.testing import grant, open_residents
from homeoa.accounts import tasks
from homeoa.core.context import association_context
from homeoa.core.models import Association, AssociationStatus
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, Unit

User = get_user_model()

BOARD_URLS = ("/", "/money/", "/units/", "/settings/roles/", "/settings/", "/account/")


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    with association_context(a):
        yield a


def _on(assoc):
    open_residents(assoc)


def _live_but_paused(assoc):
    """Live, with resident access paused, without an audit event of its own."""
    AssociationStatus.objects.create(
        association=assoc, go_live_date=dt.date(2000, 1, 1), residents_paused=True
    )


def _unit(n):
    return Unit.objects.create(number=str(n), allocated_interest=Decimal("9"))


def _owner(assoc, n=3, email="o@example.org", start=dt.date(2020, 1, 1)):
    usr = User.objects.create_user(email)
    person = Person.objects.create(full_name=email, email=email, user=usr)
    u = _unit(n)
    reg.set_initial_ownership(u, [person], start)
    return usr, person, u


def _tenant(assoc, email="t@example.org"):
    _, _, u = _owner(assoc, n=5, email="landlord@example.org")
    usr = User.objects.create_user(email)
    person = Person.objects.create(full_name=email, email=email, user=usr)
    Occupancy.objects.create(
        unit=u, person=person, kind=Occupancy.Kind.TENANT, start=dt.date(2024, 1, 1)
    )
    return usr


def _past_owner(assoc):
    usr, person, u = _owner(assoc, n=8, email="past@example.org")
    buyer = Person.objects.create(full_name="Buyer", email="buyer@example.org")
    reg.transfer_ownership(u, [buyer], dt.date(2023, 6, 1))
    return usr


# --- switch off --------------------------------------------------------------------


def test_switch_off_by_default(assoc):
    assert services.residents_enabled(assoc) is False


@pytest.mark.parametrize("kind", ["owner", "tenant", "past"])
def test_off_residents_see_only_not_open_page(client, assoc, kind):
    usr = {
        "owner": lambda: _owner(assoc)[0],
        "tenant": lambda: _tenant(assoc),
        "past": lambda: _past_owner(assoc),
    }[kind]()
    client.force_login(usr)
    resp = client.get("/me/")
    assert resp.status_code == 403 and b"not open to residents yet" in resp.content
    assert client.get("/me/pay/").content.count(b"residents-closed") == 1
    for url in BOARD_URLS:
        resp = client.get(url)
        assert resp.status_code == 302 and resp["Location"] == "/me/", url
    assert client.post("/logout/").status_code == 200


def test_off_board_unaffected(client, assoc):
    usr, _, _ = _owner(assoc)
    grant(usr, association=assoc)  # an owner who is also on the board
    client.force_login(usr)
    assert client.get("/").status_code == 200
    assert client.get("/money/").status_code == 200


def test_off_magic_link_refused_quietly(assoc, settings):
    usr, _, _ = _owner(assoc)
    board = User.objects.create_user("b@example.org")
    grant(board, association=assoc)
    tasks.send_magic_link(email=usr.email, ip=None, origin="http://t")
    assert mail.outbox == []
    tasks.send_magic_link(email=board.email, ip=None, origin="http://t")
    assert len(mail.outbox) == 1
    _on(assoc)
    tasks.send_magic_link(email=usr.email, ip=None, origin="http://t")
    assert len(mail.outbox) == 2


def test_login_page_same_for_locked_resident(client, assoc):
    usr, _, _ = _owner(assoc)
    a = client.post("/login/", {"email": usr.email})
    b = client.post("/login/", {"email": "nobody@example.org"})
    assert a.status_code == b.status_code == 200
    assert a.content.replace(usr.email.encode(), b"X") == b.content.replace(
        b"nobody@example.org", b"X"
    )


# --- switch on ---------------------------------------------------------------------


def test_on_owner_lands_on_me_with_pay(client, assoc):
    _on(assoc)
    usr, _, _ = _owner(assoc)
    client.force_login(usr)
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"] == "/me/"
    html = client.get("/me/").content.decode()
    assert 'data-tab="pay"' in html and "Unit 3" in html
    assert client.get("/me/pay/").status_code == 200
    for url in ("/money/", "/units/", "/settings/roles/"):
        assert client.get(url).status_code == 403, url


def test_on_tenant_has_no_pay_tab(client, assoc):
    _on(assoc)
    client.force_login(_tenant(assoc))
    html = client.get("/me/").content.decode()
    assert 'data-tab="pay"' not in html and 'data-tab="requests"' in html
    assert 'data-testid="tenant-note"' in html
    for url in ("/me/pay/", "/me/payments/", "/me/statements/", "/money/"):
        assert client.get(url).status_code == 403, url
    assert 'aria-label="Hubs"' in html


def test_on_past_owner_sees_no_units(client, assoc):
    _on(assoc)
    client.force_login(_past_owner(assoc))
    resp = client.get("/me/")
    assert b'data-testid="no-units"' in resp.content
    assert client.get("/me/pay/").status_code == 403
    assert client.get("/units/").status_code == 403


def test_anonymous_me_goes_to_login(client, assoc):
    resp = client.get("/me/")
    assert resp.status_code == 302 and "/login/" in resp["Location"]


# --- helpers for the roles page ----------------------------------------------------


def _board(assoc, email="b@example.org"):
    usr = User.objects.create_user(email)
    grant(usr, association=assoc)
    return usr


# --- roles page: clean() errors -------------------------------------------------------


def test_roles_page_shows_member_limit_errors(client, assoc):
    client.force_login(_board(assoc))
    for n in range(3):
        grant(
            User.objects.create_user(f"m{n}@example.org"),
            "maintenance_committee",
            association=assoc,
        )
    resp = client.post(
        "/settings/roles/assign/",
        {
            "email": "fourth@example.org",
            "role": _role_pk(assoc, "maintenance_committee"),
            "start": services.today_for(assoc).isoformat(),
        },
    )
    assert resp.status_code == 200
    assert b"at most 3 members" in resp.content
    assert b'class="errorlist nonfield"' in resp.content


def _role_pk(assoc, slug):
    from homeoa.access.models import Role

    return Role.all_associations.get(association=assoc, slug=slug).pk


def test_login_sent_notes_closed_for_everyone(client, assoc):
    usr, _, _ = _owner(assoc)
    for email in (usr.email, "nobody@example.org"):
        assert b"not open to residents yet" in client.post("/login/", {"email": email}).content
    _on(assoc)
    assert b"not open to residents yet" not in client.post("/login/", {"email": usr.email}).content


def test_e2e_residents_command(assoc, monkeypatch):
    from django.core.management import call_command

    monkeypatch.setenv("HOMEOA_E2E", "1")
    _live_but_paused(assoc)
    call_command("e2e_residents", "on")
    assoc.refresh_from_db()
    assert services.residents_enabled(assoc)
    call_command("e2e_residents", "off")
    assoc.refresh_from_db()
    assert not services.residents_enabled(assoc)
