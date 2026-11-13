"""Opt-in smoke test of the real client against the SimpleFIN beta-bridge demo.

Not part of ``scripts/check.sh`` or CI: ``addopts`` deselects the ``live_simplefin``
marker. Run it with::

    HOMEOA_ENV_FILE=/path/to/.env uv run pytest -m live_simplefin

Credentials come from the environment, or from the ``.env`` file (``HOMEOA_ENV_FILE``,
default ``<project>/.env``). A setup token can be claimed only once, so after the first
claim the access URL is appended to that ``.env`` as ``HOMEOA_SIMPLEFIN_DEMO_ACCESS_URL``
and reused. Never print or commit either value.

Bridge rules: each run makes at most 3 requests (claim only on the very first run),
with no loops or retries. Requests are counted per UTC day in ``.work/simplefin-live.json``
next to that ``.env`` (gitignored); once ``DAILY_CAP`` are used the tests skip.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from homeoa_simplefin import client as sfc
from homeoa_simplefin import sync

pytestmark = pytest.mark.live_simplefin

TOKEN = "HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN"
ACCESS = "HOMEOA_SIMPLEFIN_DEMO_ACCESS_URL"
DAILY_CAP = 6


def _env_file() -> Path:
    default = Path(__file__).resolve().parents[4] / ".env"
    return Path(os.environ.get("HOMEOA_ENV_FILE") or default)


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip().strip("\"'")
    return out


def _counter_file() -> Path:
    return _env_file().parent / ".work" / "simplefin-live.json"


def _spend() -> None:
    """Count one live request for today; skip once ``DAILY_CAP`` are used."""
    path = _counter_file()
    today = dt.datetime.now(dt.UTC).date().isoformat()
    try:
        counts = json.loads(path.read_text())
    except OSError, ValueError:
        counts = {}
    used = int(counts.get(today, 0))
    if used >= DAILY_CAP:
        pytest.skip(f"{used} live SimpleFIN requests already made today (cap {DAILY_CAP})")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({today: used + 1}))


def _creds() -> tuple[str, str]:
    file_env = _read_env(_env_file())
    get = lambda k: os.environ.get(k) or file_env.get(k, "")  # noqa: E731
    return get(TOKEN), get(ACCESS)


@pytest.fixture(scope="module")
def access_url() -> str:
    token, access = _creds()
    if not (token or access):
        pytest.skip(f"set {TOKEN} or {ACCESS} to run the live SimpleFIN smoke test")
    if access:
        return access
    _spend()
    try:
        access = sfc.SimpleFinClient().claim(token)
    except sfc.ClaimError:
        pytest.fail("setup token already claimed or rejected; set the access URL instead")
    with _env_file().open("a") as fh:
        fh.write(f"\n{ACCESS}={access}\n")
    return access


@pytest.fixture(scope="module")
def live_set(access_url):
    _spend()
    start = dt.date.today() - dt.timedelta(days=30)
    return sfc.SimpleFinClient().accounts(access_url, start)


def test_live_accounts_parse(live_set):
    aset = live_set
    assert aset.errors == []
    assert aset.accounts, "the demo bridge lists accounts"
    tz = dt.UTC
    seen_txn = False
    for a in aset.accounts:
        assert a.id and a.currency
        assert isinstance(a.balance, Decimal)
        assert a.balance_date.tzinfo is not None
        bal = sync.to_feed_balance(a, tz)
        assert isinstance(bal.amount, Decimal)
        for t in sync.to_feed_transactions(a, tz):
            seen_txn = True
            assert isinstance(t.amount, Decimal)
            assert t.external_id.startswith("simplefin:")
            assert t.posted <= dt.date.today() + dt.timedelta(days=1)
    assert seen_txn, "the demo bridge returns transactions"


def test_live_account_filter(access_url, live_set):
    first = live_set.accounts[0].id
    _spend()
    only = sfc.SimpleFinClient().accounts(access_url, account_ids=[first])
    assert [a.id for a in only.accounts] == [first]


def test_live_bad_auth(access_url):
    host = access_url.split("@", 1)[-1]
    _spend()
    with pytest.raises(sfc.AuthError):
        sfc.SimpleFinClient().accounts(f"https://wrong:creds@{host}")
