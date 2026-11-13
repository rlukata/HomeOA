import csv
import datetime as dt
import io

import pytest
from django.contrib.auth import get_user_model
from pypdf import PdfReader

from homeoa.access import permissions as perms
from homeoa.access.services import assign_role, ensure_default_roles
from homeoa.access.testing import open_residents
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.letters import batches, services
from homeoa.letters.models import LetterTemplate, MailBatch, MailItem
from homeoa.notify.models import Delivery, Notification
from homeoa.providers import get_provider
from homeoa.providers.defaults.postal import PrintBatchPostal
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership

AS_OF = dt.date(2026, 10, 7)


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row Condominium", short_name="CR")
    ensure_default_roles(a)
    services.ensure_default_templates(a)
    with association_context(a):
        yield a


def make_unit(assoc, number, owner_name, **addr):
    u = Unit.objects.create(
        association=assoc,
        number=number,
        address_line1=f"100 Cedar Row, Unit {number}",
        city="Olympia",
        region="WA",
        postal_code="98501",
    )
    owner = Person.objects.create(association=assoc, full_name=owner_name, **addr)
    set_initial_ownership(u, [owner], dt.date(2020, 1, 1))
    return u


@pytest.fixture
def letters(assoc):
    a = make_unit(
        assoc,
        "A",
        "Jordan Rivera",
        address_line1="9 Elm St",
        city="Kent",
        region="WA",
        postal_code="98030",
    )
    b = make_unit(assoc, "B", "Priya Shah")
    template = LetterTemplate.objects.get(key="reminder")
    return [services.generate_letter(template, u, as_of=AS_OF) for u in (b, a)]


def enable_residents(assoc):
    open_residents(assoc)


def test_default_provider_is_print_batch(assoc):
    assert isinstance(get_provider("PostalProvider", assoc), PrintBatchPostal)


def test_batch_build(assoc, letters):
    batch = batches.create_batch(assoc, letters, name="October reminders")
    assert batch.status == MailBatch.Status.DRAFT
    assert batch.items.count() == 2
    pdf = PdfReader(io.BytesIO(batches.file_bytes(batch, "pdf")))
    assert len(pdf.pages) == batch.pages >= 2
    assert batch.pages % 2 == 0  # each letter padded for duplex
    rows = list(csv.DictReader(io.StringIO(batches.file_bytes(batch, "csv").decode())))
    assert [r["seq"] for r in rows] == ["1", "2"]
    # Sorted by unit; owner mailing address when known, else the unit address.
    assert rows[0]["name"] == "Jordan Rivera" and rows[0]["line1"] == "9 Elm St"
    assert rows[0]["city"] == "Kent"
    assert rows[1]["name"] == "Priya Shah" and rows[1]["line1"] == "100 Cedar Row, Unit B"
    assert rows[0]["certified"] == "no"
    # Each letter is filed in its unit's comm log with a postal delivery.
    for item in batch.items.all():
        note = Notification.objects.get(object_key=f"letters.letter:{item.letter.pk}")
        assert note.unit == item.letter.unit
        assert [d.channel for d in item.deliveries.all()] == ["postal"]


def test_empty_batch_rejected(assoc):
    with pytest.raises(batches.BatchError):
        batches.create_batch(assoc, [])


def test_draft_only_while_residents_off(assoc, letters):
    batch = batches.create_batch(assoc, letters)
    assert set(Delivery.objects.values_list("status", flat=True)) == {"preview"}
    with pytest.raises(batches.ResidentsOff):
        batches.send_batch(batch)
    batch.refresh_from_db()
    assert batch.status == MailBatch.Status.DRAFT
    assert set(batch.items.values_list("status", flat=True)) == {"pending"}
    # Forcing the status does not get past the gate either.
    MailBatch.objects.filter(pk=batch.pk).update(status=MailBatch.Status.READY)
    with pytest.raises(batches.ResidentsOff):
        batches.confirm_mailed(batch)
    assert set(Delivery.objects.values_list("status", flat=True)) == {"preview"}


def test_print_batch_send_and_confirm(assoc, letters):
    enable_residents(assoc)
    batch = batches.create_batch(assoc, letters)
    batch = batches.send_batch(batch)
    assert batch.status == MailBatch.Status.READY and batch.provider == "default"
    assert set(batch.items.values_list("status", flat=True)) == {"queued_for_print"}
    assert set(Delivery.objects.values_list("status", flat=True)) == {"queued"}
    with pytest.raises(batches.BatchError):
        batches.send_batch(batch)
    batch = batches.confirm_mailed(batch)
    assert batch.status == MailBatch.Status.MAILED and batch.confirmed_at
    assert set(batch.items.values_list("status", flat=True)) == {"mailed_by_hand"}
    d = Delivery.objects.first()
    assert d.status == "sent" and d.provider_id == f"print-batch:{batch.pk}" and d.sent_at
    with pytest.raises(batches.BatchError):
        batches.confirm_mailed(batch)


def test_tracking_events_idempotent(assoc, letters):
    enable_residents(assoc)
    batch = batches.create_batch(assoc, letters)
    item = batch.items.first()
    item.status, item.provider_id = MailItem.Status.SUBMITTED, "ltr_x"
    item.save()
    assert batches.record_tracking_event(assoc, "ltr_x", batches.IN_TRANSIT).status == (
        "in_transit"
    )
    for _ in range(2):
        assert batches.record_tracking_event(assoc, "ltr_x", batches.DELIVERED).status == (
            "delivered"
        )
    # A late in-transit event never moves it back.
    assert batches.record_tracking_event(assoc, "ltr_x", batches.IN_TRANSIT).status == ("delivered")
    assert item.deliveries.get().status == "delivered"
    assert batches.record_tracking_event(assoc, "nope", batches.DELIVERED) is None


@pytest.fixture
def board_client(client, assoc):
    user = get_user_model().objects.create_user(email="board@example.org")
    assign_role(user, assoc, perms.BOARD, start=AS_OF - dt.timedelta(days=30))
    client.force_login(user)
    return client


def test_batch_views(board_client, assoc, letters):
    resp = board_client.get("/settings/letters/batches/")
    assert resp.status_code == 200
    assert "Residents are not switched on yet" in resp.content.decode()
    resp = board_client.post(
        "/settings/letters/batches/",
        {"name": "Test batch", "letters": [letter.pk for letter in letters]},
    )
    batch = MailBatch.objects.get()
    assert resp.status_code == 302 and resp.url == f"/settings/letters/batches/{batch.pk}/"
    page = board_client.get(resp.url).content.decode()
    assert "Test batch" in page and "Mark printed" not in page
    pdf = board_client.get(f"/settings/letters/batches/{batch.pk}/batch.pdf")
    assert pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert board_client.get(f"/settings/letters/batches/{batch.pk}/batch.csv").status_code == 200
    assert board_client.get(f"/settings/letters/batches/{batch.pk}/batch.exe").status_code == 404
    board_client.post(f"/settings/letters/batches/{batch.pk}/send/")
    batch.refresh_from_db()
    assert batch.status == "draft"  # gated
    enable_residents(assoc)
    board_client.post(f"/settings/letters/batches/{batch.pk}/send/")
    batch.refresh_from_db()
    assert batch.status == "ready"
    assert (
        "Mark printed"
        in board_client.get(f"/settings/letters/batches/{batch.pk}/").content.decode()
    )
    board_client.post(f"/settings/letters/batches/{batch.pk}/mailed/")
    batch.refresh_from_db()
    assert batch.status == "mailed"
    # Batched letters are no longer offered for a new batch.
    assert (
        "Every generated letter is already in a batch"
        in board_client.get("/settings/letters/batches/").content.decode()
    )


def test_batch_views_need_permission(client, assoc):
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    assert client.get("/settings/letters/batches/").status_code in (302, 403)


def test_letter_cannot_be_mailed_twice(assoc, letters):
    enable_residents(assoc)
    first = batches.create_batch(assoc, letters)
    other = batches.create_batch(assoc, letters[:1])  # drafts may overlap
    batches.send_batch(first)
    with pytest.raises(batches.BatchError):
        batches.create_batch(assoc, letters[:1])
    with pytest.raises(batches.BatchError):
        batches.send_batch(other)
    other.refresh_from_db()
    assert other.status == "draft"


@pytest.mark.parametrize("ended", ["failed", "returned"])
def test_failed_or_returned_letter_can_be_remailed(assoc, letters, ended):
    enable_residents(assoc)
    first = batches.send_batch(batches.create_batch(assoc, letters[:1]))
    old = first.items.get()
    MailItem.objects.filter(pk=old.pk).update(status=ended)
    again = batches.send_batch(batches.create_batch(assoc, letters[:1]))
    new = again.items.get()
    assert new.status == "queued_for_print"
    old_ids = set(old.deliveries.values_list("pk", flat=True))
    new_ids = set(new.deliveries.values_list("pk", flat=True))
    assert new_ids and not (old_ids & new_ids)


def test_default_name_uses_association_today(assoc, letters, monkeypatch):
    monkeypatch.setattr(batches, "association_today", lambda a: dt.date(2030, 1, 2))
    assert "2030-01-02" in batches.create_batch(assoc, letters).name
