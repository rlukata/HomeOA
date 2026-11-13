"""PLT-09: manifest, service worker (offline page only) and a11y shell hooks (PLT-08)."""

import json
import re

import pytest
from django.contrib.staticfiles import finders

pytestmark = pytest.mark.django_db


def test_manifest_served_publicly(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/manifest+json")
    data = json.loads(resp.content)
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    sizes = {i["sizes"] for i in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in data["icons"])
    for icon in data["icons"]:
        assert finders.find(icon["src"].removeprefix("/static/")), icon["src"]


def test_service_worker_served_at_root_scope(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/javascript")
    assert resp["Service-Worker-Allowed"] == "/"


def test_service_worker_does_not_cache_pages():
    """Only the offline page and its stylesheet go in the cache; navigations are never put."""
    from django.template.loader import render_to_string

    js = render_to_string("ui/sw.js")
    precache = re.search(r"var PRECACHE = \[(.*?)\];", js).group(1)
    assert "OFFLINE_URL" in precache and "base.css" in precache
    assert precache.count(",") == 1  # exactly two entries
    assert "cache.put" not in js
    assert ".add(" not in js.replace("addEventListener(", "")
    assert re.search(r"cache\.addAll\(PRECACHE\)", js)


def test_offline_page_public(client):
    resp = client.get("/offline/")
    assert resp.status_code == 200
    assert b"offline" in resp.content.lower()


def test_shell_links_manifest_and_live_region(client, django_user_model):
    user = django_user_model.objects.create_user("pwa@example.com")
    client.force_login(user)
    html = client.get("/settings/").content.decode()
    assert 'rel="manifest"' in html
    assert 'class="skip"' in html
    assert 'id="sr-announce"' in html and 'aria-live="polite"' in html
    assert 'data-sw="/sw.js"' in html


def test_login_links_manifest(client):
    html = client.get("/login/").content.decode()
    assert 'rel="manifest"' in html


def test_settings_page_has_sign_out(client, django_user_model):
    user = django_user_model.objects.create_user("signout@example.com")
    client.force_login(user)
    html = client.get("/settings/").content.decode()
    assert "Signed in as signout@example.com." in html
    assert 'action="/logout/"' in html and 'data-testid="sign-out"' in html
    client.post("/logout/")
    assert client.get("/settings/").status_code == 302
