import importlib
import re
import shutil
from pathlib import Path

import pytest
from django.core.management import call_command
from django.urls import clear_url_caches, resolve, reverse

from homeoa.ui import nav

EXPECTED_HUBS = [
    "Home",
    "Money",
    "Requests",
    "Community",
    "Governance",
    "Units",
    "Vendors",
    "Documents",
]
ROOT = Path(__file__).resolve().parent.parent


def test_registry_order_matches_prototype():
    assert [str(h.label) for h in nav.get_hubs()] == EXPECTED_HUBS
    assert [h.id for h in nav.get_hubs() if h.tabbar] == ["home", "money", "requests", "units"]


def test_subtabs_match_prototype():
    money = nav.get_hub("money")
    assert [str(t.label) for t in money.subtabs] == [
        "Overview",
        "Dues & ledger",
        "Bills",
        "Bank & deposits",
        "Budget",
        "Reports",
        "Delinquency",  # registered by statements
        "Close",  # registered by closing
    ]
    assert len(nav.get_hub("documents").subtabs) == 7  # the prototype's 6 plus Welcome packet


def test_permission_hook_filters(rf):
    hub = nav.Hub("secret", "Secret", "cog", 5, permission=lambda r: False)
    nav.register_hub(hub)
    try:
        request = rf.get("/")
        assert "secret" not in [h.id for h in nav.get_hubs(request)]
        assert "secret" in [h.id for h in nav.get_hubs()]
    finally:
        nav._registry.pop("secret")


@pytest.fixture
def debug(client, db):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("shell@example.org")
    grant(user, title="treasurer")  # Imports need finance.manage
    grant(user, "sys_admin")  # and plugins the system admin
    client.force_login(user)


def test_shell_renders_hubs_and_landmarks(client, debug):
    html = client.get("/").content.decode()
    for label in EXPECTED_HUBS:
        assert f">{label}</a>" in html
    assert 'class="hnav" aria-label="Main"' in html
    assert 'class="tabbar" aria-label="Hubs"' in html
    assert 'role="dialog"' in html and 'aria-label="More"' in html


def test_hub_and_subtab_pages(client, debug):
    resp = client.get("/money/")
    assert resp.status_code == 200
    assert "Dues &amp; ledger" in resp.content.decode()
    assert client.get("/money/bills/").status_code == 200
    assert client.get("/money/nope/").status_code == 404
    assert client.get("/nope/").status_code == 404


def test_shell_requires_login(client, db):
    resp = client.get("/money/")
    assert resp.status_code == 302 and resp["Location"] == "/login/?next=/money/"


TAG_RE = re.compile(
    r"{%\s*blocktranslate.*?{%\s*endblocktranslate\s*%}|{%.*?%}|{{.*?}}|{#.*?#}"
    r"|<script.*?</script>|<style.*?</style>|<!--.*?-->|<[^>]+>|&\w+;",
    re.S,
)


def test_templates_have_no_untranslated_text():
    offenders = []
    for path in (ROOT / "homeoa").rglob("templates/**/*.html"):
        text = TAG_RE.sub(" ", path.read_text())
        leftover = re.findall(r"[A-Za-z]{2,}", text)
        if leftover:
            offenders.append((path.name, leftover[:5]))
    assert offenders == []


@pytest.mark.skipif(shutil.which("xgettext") is None, reason="gettext not installed")
def test_makemessages_extracts(tmp_path, monkeypatch, settings):
    monkeypatch.chdir(ROOT)
    call_command(
        "makemessages",
        locale=["en"],
        ignore_patterns=[".venv", "e2e", "design", "staticfiles", ".claude"],
        no_obsolete=True,
        verbosity=0,
    )
    po = (ROOT / "locale/en/LC_MESSAGES/django.po").read_text()
    assert 'msgid "Dues & ledger"' in po
    assert 'msgid "Skip to content"' in po


URL_MAP = {
    "accounts:login": "/login/",
    "accounts:logout": "/logout/",
    "accounts:index": "/account/",
    "dashboard:index": "/",
    "dashboard:money": "/money/",
    "billing:index": "/money/assessments/",
    "payments:index": "/money/payments/",
    "payments:adjustments": "/money/adjustments/",
    "banking:index": "/money/bank/",
    "reports:index": "/money/reports/",
    "imports:index": "/units/import/",
    "imports:parallel_run": "/money/reports/parallel-run/",
    "registry:index": "/units/",
    "access:index": "/settings/roles/",
    "plugins:index": "/settings/plugins/",
    "audit:index": "/settings/audit/",
    "jobs:index": "/settings/jobs/",
}


@pytest.mark.parametrize(("name", "expected"), URL_MAP.items())
def test_app_url_prefixes(name, expected):
    assert reverse(name) == expected
    assert resolve(expected).view_name == name


@pytest.mark.parametrize("name", URL_MAP)
def test_app_placeholders_render(client, debug, name):
    assert client.get(reverse(name)).status_code == 200


def test_ui_catch_all_still_serves_unbuilt_hubs(client, debug):
    assert resolve("/governance/").view_name == "ui:hub"
    assert resolve("/requests/overview/").view_name == "ui:subtab"
    assert client.get("/governance/votes/").status_code == 200


def test_admin_mount_follows_setting(settings):
    import homeoa.urls

    try:
        settings.HOMEOA_ADMIN_ENABLED = False
        mod = importlib.reload(homeoa.urls)
        assert not any(str(p.pattern) == "admin/" for p in mod.urlpatterns)
        settings.HOMEOA_ADMIN_ENABLED = True
        mod = importlib.reload(homeoa.urls)
        assert any(str(p.pattern) == "admin/" for p in mod.urlpatterns)
    finally:
        clear_url_caches()


def test_prod_disables_admin_and_shell(monkeypatch):
    import homeoa.settings.base as base

    monkeypatch.setattr(base, "SECRET_KEY", "x")
    monkeypatch.setattr(base, "HOMEOA_FIELD_KEYS", ["Gk4mVBqb0ot2RcPdiOBDkA2gU4BAYRxQKkVvlIhs3Rk="])
    monkeypatch.delenv("HOMEOA_ADMIN_ENABLED", raising=False)
    prod = importlib.reload(importlib.import_module("homeoa.settings.prod"))
    assert prod.HOMEOA_ADMIN_ENABLED is False
    assert prod.SESSION_COOKIE_SECURE is True and prod.CSRF_COOKIE_SECURE is True
