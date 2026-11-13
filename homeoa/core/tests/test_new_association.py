"""A new install: the System admin adds the association from Home, seats the board in
Roles, and the setup pages work for it."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.models import Role
from homeoa.audit.models import AuditEvent
from homeoa.core import sandbox
from homeoa.core.models import Association
from homeoa.ledger.models import Account, Fund

User = get_user_model()

FORM = {
    "name": "Maple Court Condominium",
    "short_name": "",
    "timezone": "America/Los_Angeles",
    "fiscal_year_start_month": "7",
}


@pytest.fixture
def admin(db, client):
    user = User.objects.create_superuser("admin@example.com")
    client.force_login(user)
    return user


def test_home_sends_the_system_admin_to_add_the_association(admin, client):
    response = client.get(reverse("dashboard:index"))
    assert response.status_code == 302
    assert response["Location"] == reverse("new_association")
    page = client.get(reverse("new_association"))
    assert page.status_code == 200
    assert b"Add the association" in page.content


def test_adding_the_association_stores_it_with_roles_and_chart_of_accounts(admin, client):
    response = client.post(reverse("new_association"), FORM)
    assert response.status_code == 302
    assert response["Location"] == reverse("ui:settings")
    association = Association.objects.get()
    assert association.name == "Maple Court Condominium"
    assert association.short_name == "MCC"  # blank initials come from the name
    assert association.timezone == "America/Los_Angeles"
    assert association.fiscal_year_start_month == 7
    assert Role.all_associations.filter(association=association, slug="board").exists()
    assert Account.all_associations.filter(association=association, code="1000").exists()
    assert Fund.all_associations.filter(association=association, code="operating").exists()
    created = AuditEvent.all_associations.get(object_type="core.association", action="create")
    assert created.actor_id == admin.pk


def test_after_adding_the_system_admin_lands_on_settings(admin, client):
    page = client.post(reverse("new_association"), FORM, follow=True)
    assert page.status_code == 200
    assert b"Association added." in page.content


def test_the_system_admin_seats_the_first_board_at_once(admin, client):
    """With no board yet, a board seat applies without approval; then Home shows the setup
    steps and the board pages open."""
    client.post(reverse("new_association"), FORM)
    board = Role.all_associations.get(slug="board")
    client.post(
        reverse("access:assign"),
        {
            "email": admin.email,
            "role": board.pk,
            "title": "president",
            "start": dt.date(2026, 1, 1),
        },
    )
    home = client.get(reverse("dashboard:index"))
    assert home.status_code == 200
    assert b'data-testid="whats-next"' in home.content
    for name in ("setup:index", "governance:index", "registry:index", "billing:index"):
        assert client.get(reverse(name)).status_code == 200, name


def test_a_new_association_is_not_a_sandbox_by_default(admin, client):
    client.post(reverse("new_association"), FORM)
    assert not sandbox.is_sandbox(Association.objects.get())


def test_on_a_demo_server_a_new_association_is_a_sandbox(admin, client, settings):
    settings.HOMEOA_SANDBOX_NEW_ASSOCIATIONS = True
    client.post(reverse("new_association"), FORM)
    assert sandbox.is_sandbox(Association.objects.get())


def test_initials_typed_in_are_kept(admin, client):
    client.post(reverse("new_association"), {**FORM, "short_name": "MC"})
    assert Association.objects.get().short_name == "MC"


def test_a_time_zone_must_be_chosen(admin, client):
    response = client.post(reverse("new_association"), {**FORM, "timezone": "Mars/Base"})
    assert response.status_code == 200
    assert not Association.objects.exists()


def test_the_page_only_adds_the_first_association(admin, client):
    Association.objects.create(name="Existing", short_name="E")
    response = client.post(reverse("new_association"), FORM)
    assert response["Location"] == reverse("dashboard:index")
    assert Association.objects.count() == 1


def test_only_the_system_admin_adds_the_association(db, client):
    client.force_login(User.objects.create_user("someone@example.com"))
    assert client.get(reverse("new_association")).status_code == 403
    home = client.get(reverse("dashboard:index"))
    assert home.status_code == 200
    assert b'data-testid="not-set-up"' in home.content


@pytest.mark.parametrize(
    "name",
    [
        "ui:settings",
        "plugins:index",
        "access:index",
        "access:go_live",
    ],
)
def test_system_admin_pages_open_for_a_brand_new_association(admin, client, name):
    client.post(reverse("new_association"), FORM)
    assert client.get(reverse(name)).status_code == 200
