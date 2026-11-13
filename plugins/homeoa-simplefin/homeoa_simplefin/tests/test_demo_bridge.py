"""The sandbox may use SimpleFIN's real demo bridge, behind a flag and a host
allow-list. Synthetic tokens only; every HTTP request goes to a recording mock."""

import base64
import logging

import httpx
import pytest
from django.conf import settings as dj_settings

from homeoa.core import demo as core_demo
from homeoa.core import sandbox
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.plugins import registry
from homeoa.plugins import services as plugins
from homeoa_simplefin import sync
from homeoa_simplefin.client import SimpleFinClient
from homeoa_simplefin.demo import connect_demo
from homeoa_simplefin.fake_server import FakeClient
from homeoa_simplefin.manifest import manifest
from homeoa_simplefin.models import SimpleFinAccount

DEMO_HOST = "beta-bridge.simplefin.org"
DEMO_CLAIM = f"https://{DEMO_HOST}/simplefin/claim/SYNTHETIC-TEST-ONLY"
DEMO_TOKEN = base64.b64encode(DEMO_CLAIM.encode()).decode()
DEMO_ACCESS = f"https://synthuser:synthpass@{DEMO_HOST}/simplefin"
OTHER_CLAIM = "https://bridge.example.com/simplefin/claim/SYNTHETIC"
OTHER_TOKEN = base64.b64encode(OTHER_CLAIM.encode()).decode()

ACCOUNTS = {
    "errors": [],
    "accounts": [
        {
            "id": "DEMO-1234",
            "name": "Demo Checking",
            "org": {"name": "SimpleFIN Demo"},
            "currency": "USD",
            "balance": "100.00",
            "balance-date": 1760000000,
            "transactions": [
                {"id": "t1", "posted": 1759900000, "amount": "-5.00", "description": "x"}
            ],
        }
    ],
}


class Bridge:
    """Records every request; replaces the real HTTP transport."""

    def __init__(self, claim_status=200):
        self.requests = []
        self.claim_status = claim_status

    def handle(self, transport, request):
        self.requests.append(request)
        if request.url.host != DEMO_HOST:
            raise AssertionError(f"unexpected host {request.url.host}")
        if "/claim/" in request.url.path:
            return httpx.Response(self.claim_status, text=DEMO_ACCESS, request=request)
        return httpx.Response(200, json=ACCOUNTS, request=request)

    @property
    def claims(self):
        return [r for r in self.requests if "/claim/" in r.url.path]


@pytest.fixture(autouse=True)
def plugin_loaded():
    registry.register(manifest)


@pytest.fixture
def bridge(monkeypatch):
    b = Bridge()
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda self, req: b.handle(self, req)
    )
    monkeypatch.delenv("HOMEOA_SIMPLEFIN_FAKE", raising=False)
    return b


@pytest.fixture
def real_on(settings):
    settings.HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = True
    settings.HOMEOA_SIMPLEFIN_DEMO_HOSTS = [DEMO_HOST]
    settings.HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = DEMO_TOKEN
    return settings


@pytest.fixture
def demo(db, bridge):
    a = Association.objects.create(name="Demo", short_name="D", timezone="America/Los_Angeles")
    load_chart_of_accounts(a)
    sandbox.enable(a)
    with association_context(a):
        yield a


@pytest.fixture
def plain(db, bridge):
    a = Association.objects.create(name="Real", short_name="R", timezone="America/Los_Angeles")
    with association_context(a):
        yield a


def _inst(assoc, **data):
    plugins.save_installation(assoc, "simplefin", enabled=True, data=data)
    return plugins.installation_for(assoc, "simplefin")


# ------------------------------------------------------------------ settings


def test_test_settings_force_flag_off_and_blank_token():
    assert dj_settings.HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE is False
    assert dj_settings.HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN == ""
    import os

    assert not os.environ.get("HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN")


def test_e2e_settings_force_flag_off_and_blank_token():
    from pathlib import Path

    src = (Path(dj_settings.BASE_DIR) / "homeoa" / "settings" / "e2e.py").read_text()
    tail = src[src.rindex("HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE") - 200 :]
    assert "HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = False" in src
    assert 'HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = ""' in tail
    assert 'os.environ.pop("HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN", None)' in src


# ------------------------------------------------------------------ use_fake matrix


@pytest.mark.parametrize(
    ("flag", "url", "env_fake", "expected"),
    [
        (False, DEMO_ACCESS, False, True),
        (False, None, False, True),
        (True, DEMO_ACCESS, False, False),
        (True, DEMO_CLAIM, False, False),
        (True, "https://u:p@bridge.example.com/simplefin", False, True),
        (True, None, False, True),
        (True, DEMO_ACCESS, True, True),
    ],
)
def test_use_fake_matrix_sandbox(demo, settings, monkeypatch, flag, url, env_fake, expected):
    settings.HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = flag
    settings.HOMEOA_SIMPLEFIN_DEMO_HOSTS = [DEMO_HOST]
    if env_fake:
        monkeypatch.setenv("HOMEOA_SIMPLEFIN_FAKE", "1")
    inst = _inst(demo, access_url=url) if url else _inst(demo)
    assert sync.use_fake(inst) is expected
    assert isinstance(sync.client_for(inst), FakeClient) is expected


@pytest.mark.parametrize("flag", [False, True])
def test_use_fake_non_sandbox_unchanged(plain, settings, flag):
    settings.HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = flag
    inst = _inst(plain, access_url="https://u:p@bridge.example.com/simplefin")
    assert sync.use_fake(inst) is False
    inst.settings = {"use_fake": True}
    assert sync.use_fake(inst) is True


def test_sandbox_without_installation_uses_fake(demo, real_on):
    assert sync.use_fake(None, OTHER_CLAIM, association=demo) is True
    assert sync.use_fake(None, DEMO_CLAIM, association=demo) is False


def test_non_demo_host_refused_and_logged(demo, real_on, caplog):
    inst = _inst(demo, access_url="https://u:p@bridge.example.com/simplefin")
    with caplog.at_level(logging.WARNING):
        assert isinstance(sync.client_for(inst), FakeClient)
    assert "bridge.example.com" in caplog.text
    assert "u:p" not in caplog.text


# ------------------------------------------------------------------ claim


def test_flag_on_demo_host_claims_through_real_client(demo, real_on, bridge):
    assert connect_demo(demo) is True
    assert len(bridge.claims) == 1
    inst = plugins.installation_for(demo, "simplefin")
    assert inst.secrets["access_url"] == DEMO_ACCESS
    assert type(sync.client_for(inst)) is SimpleFinClient
    assert SimpleFinAccount.all_associations.filter(association=demo).count() == 1


def test_rerun_never_reclaims(demo, real_on, bridge):
    assert connect_demo(demo) is True
    assert connect_demo(demo) is True
    assert len(bridge.claims) == 1


def test_flag_off_no_claim(demo, real_on, bridge):
    real_on.HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE = False
    assert connect_demo(demo) is False
    assert bridge.requests == []


def test_no_token_no_claim(demo, real_on, bridge):
    real_on.HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = ""
    assert connect_demo(demo) is False
    assert bridge.requests == []


def test_non_demo_claim_host_refused(demo, real_on, bridge):
    real_on.HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = OTHER_TOKEN
    assert connect_demo(demo) is False
    assert bridge.requests == []


def test_claim_403_falls_back_without_printing_token(demo, real_on, bridge, caplog, capsys):
    bridge.claim_status = 403
    with caplog.at_level(logging.INFO):
        assert connect_demo(demo) is False
    assert len(bridge.claims) == 1
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert DEMO_TOKEN not in text
        assert "SYNTHETIC-TEST-ONLY" not in text
    assert "403" in caplog.text
    inst = plugins.installation_for(demo, "simplefin")
    assert not (inst and inst.secrets.get("access_url"))


# ------------------------------------------------------------------ demo seeding


def test_seed_path_real_bridge(demo, real_on, bridge):
    core_demo._simplefin(demo)
    inst = plugins.installation_for(demo, "simplefin")
    assert inst.secrets["access_url"] == DEMO_ACCESS
    assert len(bridge.claims) == 1


def test_seed_path_403_falls_back_to_fake(demo, real_on, bridge):
    bridge.claim_status = 403
    core_demo._simplefin(demo)
    inst = plugins.installation_for(demo, "simplefin")
    assert DEMO_HOST not in inst.secrets["access_url"]
    assert isinstance(sync.client_for(inst), FakeClient)


def test_seed_path_no_token_uses_fake_without_network(demo, real_on, bridge):
    real_on.HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN = ""
    core_demo._simplefin(demo)
    assert bridge.requests == []
    inst = plugins.installation_for(demo, "simplefin")
    assert isinstance(sync.client_for(inst), FakeClient)
