"""Owner records requests in the portal: the owner asks, the board is told, the board
marks it fulfilled and the owner is told."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import open_residents
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.notify.models import Notification
from homeoa.plugins import registry as plugin_registry
from homeoa.registry import records
from homeoa.registry.models import Person, RecordsRequest

User = get_user_model()
BOARD_EMAIL = "treasurer@cedarrow.example"
OWNER_EMAIL = "ownerc@cedarrow.example"
TENANT_EMAIL = "chris.doyle@cedarrow.example"


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


def _ask(client):
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    return client.post(
        reverse("records_portal:index"),
        {"what": "Board minutes from 2025", "delivery": "copies"},
    )


def test_owner_asks_and_the_board_is_told(pilot, client, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert _ask(client).status_code == 302
    req = RecordsRequest.objects.get()
    assert req.person.user.email == OWNER_EMAIL
    assert req.delivery == RecordsRequest.Delivery.COPIES
    assert req.received_on == dt.date(2026, 10, 7) and req.due_on == dt.date(2026, 10, 17)
    note = Notification.objects.get(
        type_key=records.RECEIVED_TYPE, recipient__user__email=BOARD_EMAIL
    )
    assert "Board minutes from 2025" in note.body
    page = client.get(reverse("records_portal:index")).content.decode()
    assert "Board minutes from 2025" in page and "Open" in page


def test_board_marks_fulfilled_and_the_owner_is_told(
    pilot, client, django_capture_on_commit_callbacks
):
    _ask(client)
    req = RecordsRequest.objects.get()
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    board_page = client.get(reverse("registry:records_requests"))
    assert board_page.status_code == 200 and "Copies by email" in board_page.content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("registry:records_request_done", args=[req.pk]))
    owner = Person.objects.get(user__email=OWNER_EMAIL)
    assert Notification.objects.filter(type_key=records.FULFILLED_TYPE, recipient=owner).exists()


def test_owner_must_say_which_records(pilot, client):
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    resp = client.post(reverse("records_portal:index"), {"what": " ", "delivery": "inspect"})
    assert resp.status_code == 400
    assert not RecordsRequest.objects.exists()


def test_tenants_do_not_ask_for_association_records(pilot, client):
    client.force_login(User.objects.get(email=TENANT_EMAIL))
    assert "more-records" not in client.get(reverse("portal:more")).content.decode()
    assert client.get(reverse("records_portal:index")).status_code == 403


def test_records_requests_tab_is_for_record_keepers(pilot, client):
    client.force_login(User.objects.get(email=OWNER_EMAIL))
    assert client.get(reverse("registry:records_requests")).status_code == 403
