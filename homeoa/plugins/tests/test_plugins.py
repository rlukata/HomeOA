import hashlib
import hmac
import json
import tomllib
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import override_settings

from homeoa.audit.models import AuditEvent
from homeoa.core.models import Association
from homeoa.events import publish
from homeoa.plugins import registry, services
from homeoa.plugins.apps import PluginsConfig
from homeoa.plugins.manifest import CORE_VERSION, PluginManifest
from homeoa.plugins.models import PluginInstallation, WebhookDelivery
from homeoa.plugins.tests.fixtures import homeoa_testplugin as tp

MOD = "homeoa.plugins.tests.fixtures.homeoa_testplugin"
EP = EntryPoint("testplugin", f"{MOD}:manifest", registry.GROUP)
BAD_EP = EntryPoint("testplugin", f"{MOD}:incompatible", registry.GROUP)


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setattr(registry, "_entry_points", lambda: [EP])
    registry.load(["testplugin"])
    services.wire_event_handlers()
    tp.received.clear()
    tp.handled.clear()
    yield tp.manifest
    services.unwire_event_handlers()
    registry.clear()


@pytest.fixture
def assoc(db):
    return Association.objects.create(name="Maple Court", short_name="MC")


def enable(assoc, **data):
    payload = {"account": "acct-1", "api_key": "s3cret", **data}
    return services.save_installation(assoc, "testplugin", enabled=True, data=payload)


# Discovery and version checks


def test_core_version_matches_pyproject():
    pyproject = tomllib.loads((Path(__file__).parents[3] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == CORE_VERSION


def test_no_plugins_imports_nothing():
    assert registry.discover([], eps=[BAD_EP]) == {}
    assert registry.django_apps([]) == []


def test_discover_filters_by_list():
    other = EntryPoint("other", f"{MOD}:does_not_exist", registry.GROUP)
    found = registry.discover(["testplugin"], eps=[EP, other])
    assert list(found) == ["testplugin"]


def test_missing_listed_plugin_errors():
    with pytest.raises(registry.PluginError, match="nope"):
        registry.discover(["nope"], eps=[EP])


def test_incompatible_core_refused_with_message():
    with pytest.raises(registry.PluginError, match=r"requires HomeOA core >=99.*core 0\.1\.0"):
        registry.discover(["testplugin"], eps=[BAD_EP])


def test_startup_refuses_incompatible(monkeypatch):
    monkeypatch.setattr(registry, "_entry_points", lambda: [BAD_EP])
    import homeoa.plugins as pkg

    config = PluginsConfig("homeoa.plugins", pkg)
    with override_settings(HOMEOA_PLUGINS=["testplugin"]), pytest.raises(ImproperlyConfigured):
        config.ready()
    registry.clear()


def test_not_a_manifest_errors():
    ep = EntryPoint("testplugin", f"{MOD}:Settings", registry.GROUP)
    with pytest.raises(registry.PluginError, match="PluginManifest"):
        registry.discover(["testplugin"], eps=[ep])


def test_manifest_validates_id_and_secret_fields():
    with pytest.raises(ValueError):
        PluginManifest(id="Bad-Id", name="x", version="1")
    with pytest.raises(ValueError):
        PluginManifest(
            id="ok_id", name="x", version="1", settings_model=tp.Settings, secret_fields=("zz",)
        )


def test_providers_listing(plugin):
    assert registry.providers("TestProvider") == [("testplugin", f"{MOD}.Webhook")]
    assert registry.providers("Other") == []


# Settings validation and secrets


def test_schema_validation(plugin, assoc):
    with pytest.raises(services.SettingsError) as exc:
        enable(assoc, account="ab")
    assert "account" in exc.value.errors
    with pytest.raises(services.SettingsError):
        services.save_installation(assoc, "testplugin", enabled=True, data={"account": "abcd"})


def test_secrets_encrypted_and_kept_when_blank(plugin, assoc):
    inst = enable(assoc)
    assert inst.settings == {"account": "acct-1", "limit": 10}
    assert inst.secrets == {"api_key": "s3cret"}
    with connection.cursor() as cur:
        cur.execute("SELECT secrets FROM homeoa_plugins_plugininstallation WHERE id=%s", [inst.pk])
        raw = cur.fetchone()[0]
    assert "s3cret" not in raw
    inst = services.save_installation(
        assoc, "testplugin", enabled=True, data={"account": "acct-2", "api_key": ""}
    )
    inst.refresh_from_db()
    assert inst.secrets == {"api_key": "s3cret"}
    assert inst.settings["account"] == "acct-2"


def test_enable_without_valid_settings_refused(plugin, assoc):
    with pytest.raises(services.SettingsError):
        services.save_installation(assoc, "testplugin", enabled=True)


def test_changes_are_audited_without_secret_values(plugin, assoc):
    enable(assoc)
    services.save_installation(assoc, "testplugin", enabled=False)
    events = AuditEvent.all_associations.filter(object_type="homeoa_plugins.plugininstallation")
    assert events.count() == 2
    assert "s3cret" not in json.dumps([e.changes for e in events])
    last = events.order_by("-pk").first()
    assert "enabled" in last.changes


# UI


@pytest.fixture
def shell(client, assoc):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("admin@example.org")
    grant(user, "sys_admin", association=assoc)  # plugins are the system admin's
    client.force_login(user)


def test_list_page_shows_builtins_only(client, shell, assoc):
    # With no plugins loaded, only core-shipped (built-in) manifests are listed.
    resp = client.get("/settings/plugins/")
    assert resp.status_code == 200
    assert b"Email bounces" in resp.content


def test_ui_enable_validates_and_hides_secret(client, shell, plugin, assoc):
    assert b"Test plugin" in client.get("/settings/plugins/").content
    url = "/settings/plugins/testplugin/"
    bad = client.post(url, {"enabled": "on", "account": "ab", "api_key": "k"})
    assert bad.status_code == 200
    assert not PluginInstallation.all_associations.exists()
    ok = client.post(url, {"enabled": "on", "account": "acct-9", "api_key": "topsecret"})
    assert ok.status_code == 302
    page = client.get(url)
    assert b"topsecret" not in page.content
    assert b"acct-9" in page.content
    assert services.is_enabled(assoc.pk, "testplugin")


def test_unknown_plugin_page_404(client, shell, assoc):
    assert client.get("/settings/plugins/nope/").status_code == 404


# Event handlers


def test_handler_receives_ping_only_when_enabled(plugin, assoc):
    publish(tp.TestPing(association_id=assoc.pk))
    assert tp.received == []
    enable(assoc)
    publish(tp.TestPing(association_id=assoc.pk))
    assert len(tp.received) == 1


# Webhooks


def _post(client, assoc, body, key="s3cret"):
    raw = json.dumps(body).encode()
    sig = hmac.new(key.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        f"/hooks/testplugin/{assoc.pk}/",
        raw,
        content_type="application/json",
        headers={"X-Signature": sig},
    )


def test_webhook_bad_signature_400(client, plugin, assoc):
    enable(assoc)
    assert _post(client, assoc, {"id": "evt_1"}, key="wrong").status_code == 400
    assert tp.handled == []


def test_webhook_replay_processed_once(client, plugin, assoc):
    enable(assoc)
    assert _post(client, assoc, {"id": "evt_1"}).status_code == 200
    assert _post(client, assoc, {"id": "evt_1"}).status_code == 200
    assert tp.handled == ["evt_1"]
    assert WebhookDelivery.all_associations.count() == 1


def test_webhook_disabled_or_unknown_404(client, plugin, assoc):
    assert _post(client, assoc, {"id": "e"}).status_code == 404
    assert client.post("/hooks/nope/1/").status_code == 404


def test_webhook_handler_failure_allows_retry(client, plugin, assoc, monkeypatch):
    enable(assoc)

    def boom(self, request, installation):
        raise RuntimeError("down")

    monkeypatch.setattr(tp.Webhook, "handle", boom)
    assert _post(client, assoc, {"id": "evt_2"}).status_code == 500
    assert not WebhookDelivery.all_associations.exists()


def test_anonymous_redirected_to_login(client, assoc):
    resp = client.get("/settings/plugins/")
    assert resp.status_code == 302
    assert resp["Location"].startswith("/login/")


def test_webhook_writes_attributed_to_plugin(client, plugin, assoc):
    enable(assoc)
    assert _post(client, assoc, {"id": "evt_9"}).status_code == 200
    event = AuditEvent.all_associations.get(action="testplugin.webhook")
    assert event.actor_label == "webhook:testplugin"
