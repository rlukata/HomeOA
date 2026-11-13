import time
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core import mail, signing

from homeoa import providers
from homeoa.access.testing import grant
from homeoa.core.models import Association
from homeoa.plugins import registry, services
from homeoa.plugins.manifest import PluginManifest
from homeoa.providers.bankfeed import BankFeedProvider
from homeoa.providers.defaults import localdisk
from homeoa.providers.defaults.localdisk import LocalDiskStorage
from homeoa.providers.email import EmailChannel, EmailRequest, EmailResult
from homeoa.providers.esign import SignatureProvider
from homeoa.providers.importer import Importer
from homeoa.providers.payments import PaymentProvider, PaymentRequest, PaymentStatus
from homeoa.providers.postal import PostalProvider
from homeoa.providers.storage import FileNotFound, FileStorage
from homeoa.rules.base import RulePack


class FakeEmail(EmailChannel):
    def __init__(self, installation):
        self.installation = installation

    def send(self, message):
        return EmailResult(sent=True, message_id="fake")


FAKE = PluginManifest(
    id="fakemail",
    name="Fake mail",
    version="1",
    providers={"EmailChannel": f"{__name__}.FakeEmail"},
)


@pytest.fixture
def assoc(db):
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def fake_plugin():
    registry.register(FAKE)
    yield FAKE
    registry.clear()


@pytest.fixture
def storage(tmp_path, settings):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    return LocalDiskStorage()


@pytest.mark.parametrize(
    ("kind", "iface"),
    [
        ("PaymentProvider", PaymentProvider),
        ("BankFeedProvider", BankFeedProvider),
        ("EmailChannel", EmailChannel),
        ("PostalProvider", PostalProvider),
        ("SignatureProvider", SignatureProvider),
        ("Importer", Importer),
        ("FileStorage", FileStorage),
        ("RulePack", RulePack),
    ],
)
def test_every_interface_has_a_default(assoc, kind, iface):
    assert isinstance(providers.get_provider(kind, assoc), iface)
    assert isinstance(providers.get(iface, None), iface)


def test_unknown_interface():
    with pytest.raises(KeyError):
        providers.get_provider("Nope", None)


def test_plugin_overrides_only_when_enabled(assoc, fake_plugin):
    assert type(providers.get_provider("EmailChannel", assoc)).__name__ == "SmtpEmail"
    inst = services.save_installation(assoc, "fakemail", enabled=True, data={})
    got = providers.get_provider(EmailChannel, assoc)
    assert isinstance(got, FakeEmail)
    assert got.installation == inst
    other = Association.objects.create(name="Other", short_name="OT")
    assert type(providers.get_provider("EmailChannel", other)).__name__ == "SmtpEmail"
    services.save_installation(assoc, "fakemail", enabled=False)
    assert type(providers.get_provider("EmailChannel", assoc)).__name__ == "SmtpEmail"


def test_manual_payments(assoc):
    p = providers.get_provider("PaymentProvider", assoc)
    res = p.collect(PaymentRequest(association_id=assoc.pk, unit_id=1, amount=Decimal("10.00")))
    assert res.status is PaymentStatus.MANUAL
    assert p.online is False


def test_smtp_email_sends(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    res = providers.get_default("EmailChannel").send(
        EmailRequest(to=("a@example.org",), subject="Hi", text="Body", html="<p>Body</p>")
    )
    assert res.sent and res.message_id
    assert mail.outbox[0].subject == "Hi"


def test_csv_mapper_preview():
    imp = providers.get_default("Importer")
    out = imp.preview("units", b"Unit,Owner\n1,Ana\n", {"number": "Unit"})
    assert out.rows == ({"number": "1"},)
    assert imp.preview("units", b"Unit\n1\n", {"x": "Missing"}).errors


def test_bankfeed_default_parses_csv():
    txns = providers.get_default("BankFeedProvider").parse_file(
        "a.csv", b"Date,Description,Amount\n2026-10-03,X,1.50\n"
    )
    assert [t.amount for t in txns] == [Decimal("1.50")]


# Storage


def test_storage_put_get_delete(storage):
    f = storage.put(7, "../lease agreement.pdf", b"%PDF-1", folder="registry/leases")
    assert f.key.startswith("7/registry/leases/")
    assert ".." not in f.key and f.size == 6
    assert storage.exists(f.key)
    assert storage.get(f.key) == b"%PDF-1"
    storage.delete(f.key)
    assert not storage.exists(f.key)
    storage.delete(f.key)
    with pytest.raises(FileNotFound):
        storage.get(f.key)


@pytest.mark.parametrize("key", ["../etc/passwd", "7/../../x", "/abs", "x/" + "a" * 32 + "-f"])
def test_storage_rejects_bad_keys(storage, key):
    with pytest.raises(FileNotFound):
        storage.get(key)
    assert storage.exists(key) is False


def test_signed_url_downloads_then_expires(storage, client, monkeypatch, db):
    f = storage.put(1, "minutes.txt", b"hello")
    url = storage.signed_url(f.key, association_id=1, expires_in=60, filename="Minutes.txt")
    assert url.startswith("/files/")
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert 'filename="Minutes.txt"' in resp["Content-Disposition"]
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["Referrer-Policy"] == "no-referrer"
    now = time.time()
    monkeypatch.setattr(localdisk.time, "time", lambda: now + 61)
    assert client.get(url).status_code == 404


def test_signed_url_tampered(storage, client, db):
    f = storage.put(1, "a.txt", b"x")
    url = storage.signed_url(f.key, association_id=1)
    assert client.get(url[:-3] + "AAA/").status_code == 404
    storage.delete(f.key)
    assert client.get(url).status_code == 404


def test_signed_url_expiry_capped(storage, client, monkeypatch, db):
    f = storage.put(1, "a.txt", b"x")
    url = storage.signed_url(f.key, association_id=1, expires_in=86400)
    now = time.time()
    monkeypatch.setattr(localdisk.time, "time", lambda: now + 301)
    assert client.get(url).status_code == 404


def test_signed_url_bound_to_association(storage, client, db):
    f = storage.put(1, "a.txt", b"x")
    with pytest.raises(FileNotFound):
        storage.signed_url(f.key, association_id=2)
    forged = signing.dumps(
        {"k": f.key, "a": 2, "f": "", "x": int(time.time()) + 60}, salt=localdisk.SALT
    )
    assert client.get(f"/files/{forged}/").status_code == 404
    no_assoc = signing.dumps({"k": f.key, "f": "", "x": int(time.time()) + 60}, salt=localdisk.SALT)
    assert client.get(f"/files/{no_assoc}/").status_code == 404


# Deterministic resolution


class OtherEmail(FakeEmail):
    pass


FAKE2 = PluginManifest(
    id="othermail",
    name="Other mail",
    version="1",
    providers={"EmailChannel": f"{__name__}.OtherEmail"},
)


@pytest.fixture
def two_plugins():
    registry.register(FAKE)
    registry.register(FAKE2)
    yield
    registry.clear()


def kind_of(assoc):
    return type(providers.get_provider("EmailChannel", assoc)).__name__


def test_single_enabled_plugin_is_used(assoc, two_plugins):
    services.save_installation(assoc, "othermail", enabled=True, data={})
    assert kind_of(assoc) == "OtherEmail"
    assert providers.warnings(assoc) == []


def test_several_enabled_fall_back_with_warning(assoc, two_plugins):
    services.save_installation(assoc, "fakemail", enabled=True, data={})
    services.save_installation(assoc, "othermail", enabled=True, data={})
    assert kind_of(assoc) == "SmtpEmail"
    assert providers.warnings(assoc) == [
        {"kind": "EmailChannel", "reason": "ambiguous", "candidates": ["fakemail", "othermail"]}
    ]
    providers.set_choice(assoc, "EmailChannel", "othermail")
    assert kind_of(assoc) == "OtherEmail"
    assert providers.warnings(assoc) == []
    providers.set_choice(assoc, "EmailChannel", "default")
    assert kind_of(assoc) == "SmtpEmail"
    assert providers.warnings(assoc) == []


def test_chosen_plugin_turned_off(assoc, two_plugins):
    services.save_installation(assoc, "fakemail", enabled=True, data={})
    providers.set_choice(assoc, "EmailChannel", "fakemail")
    services.save_installation(assoc, "fakemail", enabled=False)
    services.save_installation(assoc, "othermail", enabled=True, data={})
    assert kind_of(assoc) == "SmtpEmail"  # never silently switches to another plugin
    assert providers.warnings(assoc)[0]["reason"] == "chosen_off"


def test_set_choice_validates_and_audits(assoc, two_plugins):
    from homeoa.audit.models import AuditEvent

    with pytest.raises(ValueError):
        providers.set_choice(assoc, "EmailChannel", "nope")
    providers.set_choice(assoc, "EmailChannel", "fakemail")
    assert assoc.settings["providers"] == {"EmailChannel": "fakemail"}
    assert AuditEvent.all_associations.filter(action="providers.choice").exists()


def test_plugins_page_choice_and_warning(client, assoc, two_plugins):
    services.save_installation(assoc, "fakemail", enabled=True, data={})
    services.save_installation(assoc, "othermail", enabled=True, data={})
    user = get_user_model().objects.create_user("admin@example.org")
    grant(user, "sys_admin", association=assoc)  # plugins are the system admin's
    client.force_login(user)
    html = client.get("/settings/plugins/").content.decode()
    assert 'data-testid="provider-warning"' in html
    assert "Use for: Email" in html
    resp = client.post("/settings/providers/", {"kind": "EmailChannel", "plugin_id": "fakemail"})
    assert resp.status_code == 302
    assoc.refresh_from_db()
    assert kind_of(assoc) == "FakeEmail"
    html = client.get("/settings/plugins/").content.decode()
    assert 'data-testid="provider-warning"' not in html
    assert client.post("/settings/providers/", {"kind": "Bogus"}).status_code == 404
