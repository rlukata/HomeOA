import datetime as dt

import pytest

from homeoa import providers
from homeoa.access.testing import close_residents, open_residents
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.letters import batches, services
from homeoa.letters.models import LetterTemplate, MailBatch
from homeoa.notify.models import Delivery
from homeoa.plugins import registry
from homeoa.plugins import services as plugins
from homeoa_lob import fake
from homeoa_lob.manifest import manifest
from homeoa_lob.provider import LobPostal
from homeoa_lob.webhook import SIGNATURE_HEADER, TIMESTAMP_HEADER

SECRET = "lob-test-secret-0123456789"
AS_OF = dt.date(2026, 10, 7)


@pytest.fixture(autouse=True)
def plugin_loaded():
    registry.register(manifest)
    fake.FakeLob.reset()
    yield


@pytest.fixture
def assoc(db, settings, tmp_path):
    from homeoa.registry.models import Person, Unit
    from homeoa.registry.services import set_initial_ownership

    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    open_residents(a)
    services.ensure_default_templates(a)
    with association_context(a):
        for n in ("A", "B"):
            u = Unit.objects.create(
                association=a,
                number=n,
                address_line1=f"100 Cedar Row, Unit {n}",
                city="Olympia",
                region="WA",
                postal_code="98501",
            )
            p = Person.objects.create(association=a, full_name=f"Owner {n}")
            set_initial_ownership(u, [p], dt.date(2020, 1, 1))
        yield a


@pytest.fixture
def lob_on(assoc):
    plugins.save_installation(assoc, "lob", enabled=True, data={"webhook_secret": SECRET})
    return assoc


def make_batch(assoc, certified=False):
    from homeoa.registry.models import Unit

    template = LetterTemplate.objects.get(key="reminder")
    letters = [services.generate_letter(template, u, as_of=AS_OF) for u in Unit.objects.all()]
    return batches.create_batch(assoc, letters, certified=certified)


def post_event(client, assoc, letter_id, event_type, event_id="", secret=SECRET):
    body = fake.event(letter_id, event_type, event_id)
    stamp = "1760000000"
    return client.post(
        f"/hooks/lob/{assoc.pk}/",
        body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: fake.sign(body, stamp, secret), TIMESTAMP_HEADER: stamp},
    )


def test_resolves_only_when_enabled(assoc):
    assert not isinstance(providers.get_provider("PostalProvider", assoc), LobPostal)
    plugins.save_installation(assoc, "lob", enabled=True, data={"webhook_secret": SECRET})
    assert isinstance(providers.get_provider("PostalProvider", assoc), LobPostal)


def test_fake_send_certified(lob_on):
    batch = batches.send_batch(make_batch(lob_on, certified=True))
    assert batch.status == MailBatch.Status.SUBMITTED and batch.provider == "lob"
    items = list(batch.items.all())
    assert all(i.status == "submitted" and i.provider_id.startswith("ltr_") for i in items)
    assert all(i.tracking.startswith("9407") for i in items)
    sent = fake.FakeLob.letters[items[0].provider_id]
    assert sent["extra_service"] == "certified" and sent["to"]["address_city"] == "Olympia"
    deliveries = Delivery.objects.filter(channel="postal")
    assert {d.status for d in deliveries} == {"sent"}
    assert {d.provider_id for d in deliveries} == {i.provider_id for i in items}


def test_fake_send_not_certified_has_no_tracking(lob_on):
    batch = batches.send_batch(make_batch(lob_on))
    assert all(i.tracking == "" for i in batch.items.all())


def test_send_failure_marks_failed(lob_on):
    class Broken:
        def create_letter(self, **kw):
            raise RuntimeError("address undeliverable")

    batch = make_batch(lob_on)
    item = batch.items.first()
    result = LobPostal(client=Broken()).send_letter(batches.letter_request(item))
    assert result.status == "failed" and "undeliverable" in result.message


def test_draft_only_while_residents_off(lob_on):
    close_residents(lob_on)
    batch = make_batch(lob_on)
    with pytest.raises(batches.ResidentsOff):
        batches.send_batch(batch)
    assert fake.FakeLob.letters == {}
    batch.refresh_from_db()
    assert batch.status == "draft"


def test_webhook_updates_status_idempotently(client, lob_on):
    batch = batches.send_batch(make_batch(lob_on))
    item = batch.items.first()
    assert post_event(client, lob_on, item.provider_id, "letter.in_transit").status_code == 200
    item.refresh_from_db()
    assert item.status == "in_transit"
    resp = post_event(client, lob_on, item.provider_id, "letter.delivered", "evt_1")
    assert resp.status_code == 200
    again = post_event(client, lob_on, item.provider_id, "letter.delivered", "evt_1")
    assert again.content == b"duplicate"
    item.refresh_from_db()
    assert item.status == "delivered"
    d = item.deliveries.get()
    assert d.status == "delivered" and d.delivered_at


def test_webhook_returned(client, lob_on):
    batch = batches.send_batch(make_batch(lob_on, certified=True))
    item = batch.items.first()
    post_event(client, lob_on, item.provider_id, "letter.certified.returned_to_sender")
    item.refresh_from_db()
    assert item.status == "returned"
    assert item.deliveries.get().status == "failed"


def test_webhook_rejects_bad_signature(client, lob_on):
    batch = batches.send_batch(make_batch(lob_on))
    item = batch.items.first()
    resp = post_event(client, lob_on, item.provider_id, "letter.delivered", secret="wrong" * 5)
    assert resp.status_code == 400
    item.refresh_from_db()
    assert item.status == "submitted"


def test_unknown_event_ignored(client, lob_on):
    batch = batches.send_batch(make_batch(lob_on))
    item = batch.items.first()
    assert post_event(client, lob_on, item.provider_id, "letter.created").status_code == 200
    item.refresh_from_db()
    assert item.status == "submitted"
