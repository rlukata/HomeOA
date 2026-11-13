"""E2E harness for HomeOA. See e2e/README.md and e2e/REQUIREMENTS.md.

Imports only stdlib + pytest + playwright at module level so collection works
before the Django skeleton lands. Django is touched only through
`manage.py` subprocesses, so the server and the tests never share a process.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
REPO = E2E_DIR.parent
MANAGE = REPO / "manage.py"
sys.path.insert(0, str(E2E_DIR))

# TODO: confirm the settings module name. We ask for homeoa.settings.e2e.
FIXED_TODAY = "2026-10-07"  # seed_demo "today"; HOMEOA_FIXED_TODAY
SETTINGS = os.environ.get("HOMEOA_E2E_SETTINGS", "homeoa.settings.e2e")
BASE_URL_ENV = "HOMEOA_E2E_BASE_URL"  # set to reuse an already running server
SEED_COMMAND = ["seed_demo", "--reset"]

# Seeded users (see homeoa/core/demo.py).
BOARD_EMAIL = os.environ.get("HOMEOA_E2E_BOARD_EMAIL", "treasurer@cedarrow.example")
SECRETARY_EMAIL = os.environ.get("HOMEOA_E2E_SECRETARY_EMAIL", "secretary@cedarrow.example")
OWNER_EMAIL = os.environ.get("HOMEOA_E2E_OWNER_EMAIL", "owner7@cedarrow.example")
TENANT_EMAIL = os.environ.get("HOMEOA_E2E_TENANT_EMAIL", "chris.doyle@cedarrow.example")


def pytest_configure(config):
    config.addinivalue_line("markers", "phone: runs at the 390px phone viewport")
    config.addinivalue_line("markers", "mutates: writes data; DB is re-seeded before it")


# ---------------------------------------------------------------- manage.py


def env() -> dict[str, str]:
    e = dict(os.environ)
    e["DJANGO_SETTINGS_MODULE"] = SETTINGS
    e.setdefault("HOMEOA_E2E", "1")
    # Pin the server's "today" to the seeded demo date so date-dependent views are stable.
    e.setdefault("HOMEOA_FIXED_TODAY", FIXED_TODAY)
    return e


def manage(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a management command against the E2E database."""
    return subprocess.run(
        ["uv", "run", "python", str(MANAGE), *args],
        cwd=REPO,
        env=env(),
        text=True,
        capture_output=True,
        check=check,
    )


def _ensure_db() -> None:
    """(Re)create the E2E database on the shared compose Postgres."""
    import psycopg

    name = os.environ.get("HOMEOA_E2E_DB_NAME", "homeoa_e2e")
    conn = psycopg.connect(
        host=os.environ.get("HOMEOA_DB_HOST", "localhost"),
        port=os.environ.get("HOMEOA_DB_PORT", "5432"),
        user=os.environ.get("HOMEOA_DB_USER", "homeoa"),
        password=os.environ.get("HOMEOA_DB_PASSWORD", "homeoa"),
        dbname="homeoa",
        autocommit=True,
    )
    with conn:
        # Fresh DB every session (it is test-only); HOMEOA_E2E_KEEP_DB=1 reuses it.
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        if exists and os.environ.get("HOMEOA_E2E_KEEP_DB") != "1":
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            exists = None
        if not exists:
            conn.execute(f'CREATE DATABASE "{name}"')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:  # noqa: BLE001 - any failure means "not up yet"
            time.sleep(0.3)
    raise RuntimeError(f"server did not come up at {url}")


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def live_server_url():
    """Start `runserver` on a free port against the E2E DB, or reuse $HOMEOA_E2E_BASE_URL."""
    if url := os.environ.get(BASE_URL_ENV):
        yield url.rstrip("/")
        return
    if not MANAGE.exists():
        pytest.skip("no manage.py found")
    _ensure_db()
    r = manage("migrate", "--noinput", check=False)
    if r.returncode:
        pytest.fail(f"migrate failed: {r.stderr[-2000:]}")
    port = _free_port()
    proc = subprocess.Popen(
        ["uv", "run", "python", str(MANAGE), "runserver", f"127.0.0.1:{port}", "--noreload"],
        cwd=REPO,
        env=env(),
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_http(url + "/healthz")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def base_url(live_server_url):
    """pytest-playwright reads `base_url`, so page.goto("/units/") works."""
    return live_server_url


_SEED_AVAILABLE: bool | None = None

_BOOTSTRAP = f"""
from django.contrib.auth import get_user_model
u, _ = get_user_model().objects.get_or_create(email={BOARD_EMAIL!r})
u.is_active = True
u.save()
# No seed_demo --reset yet: undo MFA state a previous test left behind.
from homeoa.accounts.models import TOTPDevice
from homeoa.core.models import Association
TOTPDevice.objects.filter(user=u).delete()
for a in Association.objects.all():
    if (a.settings or {{}}).pop("require_mfa_for_board", None) is not None:
        a.save(update_fields=["settings"])
"""


def _ensure_bootstrap_users() -> None:
    """Minimal stand-in for seed_demo: just the treasurer login."""
    r = manage("shell", "-c", _BOOTSTRAP, check=False)
    if r.returncode != 0:
        pytest.fail(f"bootstrap user failed: {r.stderr[-2000:]}")
    r = manage(
        "grant_role",
        BOARD_EMAIL,
        "board",
        "--title",
        "treasurer",
        "--create-association",
        "Cedar Row",
        check=False,
    )
    if r.returncode != 0:
        pytest.fail(f"grant_role for {BOARD_EMAIL} failed: {r.stdout[-1000:]} {r.stderr[-2000:]}")


def _has_seed_command() -> bool:
    global _SEED_AVAILABLE
    if _SEED_AVAILABLE is None:
        _SEED_AVAILABLE = manage("help", SEED_COMMAND[0], check=False).returncode == 0
    return _SEED_AVAILABLE


@pytest.fixture
def seed(live_server_url):
    """Reset the E2E DB to the Cedar Row sample. Request it in every test."""

    def _seed():
        if not _has_seed_command():
            _ensure_bootstrap_users()  # TODO: remove once seed_demo exists
            return
        r = manage(*SEED_COMMAND, check=False)
        if r.returncode != 0:
            pytest.fail(f"seed failed: {r.stderr[-2000:]}")

    _seed()
    return _seed


@pytest.fixture
def seed_bare(live_server_url):
    """Association + board users only, no units/people/ledger (seed_demo --bare)."""
    if not _has_seed_command():
        pytest.skip("seed_demo not available yet")
    r = manage(*SEED_COMMAND, "--bare", check=False)
    if r.returncode != 0:
        pytest.fail(f"bare seed failed: {r.stderr[-2000:]}")


@pytest.fixture
def mail():
    from helpers.mail import Mailbox

    box = Mailbox.from_env()
    box.clear()
    return box


@pytest.fixture
def login(page, base_url):
    """login(email): sign in without the inbox, via the e2e_login_url command."""
    from helpers.auth import login_as

    return lambda email, p=None: login_as(p or page, base_url, email)


@pytest.fixture
def phone_page(browser, base_url):
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=3,
        is_mobile=True,
        has_touch=True,
        base_url=base_url,
    )
    p = ctx.new_page()
    yield p
    ctx.close()


@pytest.fixture
def axe():
    from helpers.a11y import assert_no_serious_violations

    return assert_no_serious_violations


# --- CSP guard -----------------------------------------------------
# Every browser context records securitypolicyviolation events and console CSP
# errors; each test fails at teardown if any were seen.
_CSP_INIT = """
document.addEventListener('securitypolicyviolation', e => {
  console.error('CSP-VIOLATION ' + e.violatedDirective + ' ' + (e.blockedURI || 'inline')
    + ' at ' + e.sourceFile + ':' + e.lineNumber);
});
"""
_csp_seen: list[str] = []


def _watch_csp(ctx) -> None:
    ctx.add_init_script(_CSP_INIT)

    def on_console(msg):
        text = msg.text
        if msg.type == "error" and ("CSP-VIOLATION" in text or "Content Security Policy" in text):
            _csp_seen.append(f"{msg.page.url if msg.page else '?'}: {text}")

    ctx.on("console", on_console)


@pytest.fixture(scope="session", autouse=True)
def _csp_patch_contexts():
    from playwright.sync_api import Browser

    orig = Browser.new_context

    def new_context(self, *a, **kw):
        ctx = orig(self, *a, **kw)
        _watch_csp(ctx)
        return ctx

    Browser.new_context = new_context
    yield
    Browser.new_context = orig


@pytest.fixture(autouse=True)
def csp_guard():
    _csp_seen.clear()
    yield
    seen, _csp_seen[:] = list(_csp_seen), []
    assert not seen, "CSP violations:\n" + "\n".join(seen)
