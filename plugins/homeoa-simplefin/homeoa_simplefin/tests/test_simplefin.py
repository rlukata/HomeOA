import base64
import datetime as dt
from decimal import Decimal

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from homeoa import providers
from homeoa.audit.models import AuditEvent
from homeoa.banking.models import BankAccount, BankTransaction, ImportBatch, StatementBalance
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry
from homeoa.plugins import registry
from homeoa.plugins import services as plugins
from homeoa.plugins.models import PluginInstallation
from homeoa_simplefin import client as sfc
from homeoa_simplefin import fake_server, sync, todos
from homeoa_simplefin.fake_server import FakeClient
from homeoa_simplefin.manifest import manifest
from homeoa_simplefin.models import Connection, SimpleFinAccount
from homeoa_simplefin.provider import SimpleFinFeed

D = Decimal


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real HTTP attempt fails the test."""

    def boom(self, request):
        raise AssertionError(f"network call to {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.delenv("HOMEOA_SIMPLEFIN_FAKE", raising=False)


@pytest.fixture(autouse=True)
def plugin_loaded():
    registry.register(manifest)
    yield


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def operating(assoc):
    return BankAccount.objects.create(
        association=assoc,
        name="Operating checking",
        account_number="000123454321",
        gl_account=Account.objects.get(code="1000"),
        fund=Fund.objects.get(code="operating"),
    )


@pytest.fixture
def connected(assoc, operating):
    """Fake-connected and the operating account linked."""
    plugins.save_installation(assoc, "simplefin", enabled=True, data={"use_fake": True})
    result = sync.connect(assoc, fake_server.SETUP_TOKEN, client=FakeClient())
    assert result.ok and result.unmapped == 2
    sync.map_account(SimpleFinAccount.objects.get(sf_id="ACT-OPER-4321"), operating)
    return assoc


# ------------------------------------------------------------------ protocol / claim


def test_decode_setup_token():
    assert sfc.decode_setup_token(fake_server.SETUP_TOKEN) == fake_server.CLAIM_URL
    with pytest.raises(sfc.ClaimError):
        sfc.decode_setup_token("not base64 !!")
    with pytest.raises(sfc.ClaimError):
        sfc.decode_setup_token(base64.b64encode(b"http://insecure/claim").decode())


def test_claim_returns_access_url_and_bad_token_fails():
    c = FakeClient()
    assert c.claim(fake_server.SETUP_TOKEN) == fake_server.ACCESS_URL
    other = base64.b64encode(f"https://{fake_server.HOST}/simplefin/claim/used".encode())
    with pytest.raises(sfc.ClaimError):
        c.claim(other.decode())


def test_connect_stores_access_url_encrypted(assoc):
    sync.connect(assoc, fake_server.SETUP_TOKEN, client=FakeClient())
    inst = plugins.installation_for(assoc, "simplefin")
    assert inst.enabled
    assert inst.secrets["access_url"] == fake_server.ACCESS_URL
    assert "access_url" not in inst.settings
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "SELECT secrets::text FROM homeoa_plugins_plugininstallation WHERE id=%s", [inst.pk]
        )
        raw = cur.fetchone()[0]
    assert "fake-secret" not in raw
    # Discovery: accounts are listed, none linked, nothing imported.
    assert SimpleFinAccount.objects.count() == 2
    assert BankTransaction.objects.count() == 0
    assert AuditEvent.objects.filter(action="simplefin.connect").exists()


def test_parse_accounts_mapping_skips_pending_and_uses_local_dates():
    aset = FakeClient().accounts(fake_server.ACCESS_URL, dt.date(2026, 9, 1))
    oper = next(a for a in aset.accounts if a.id == "ACT-OPER-4321")
    tz = sync._tz(Association(timezone="America/Los_Angeles"))
    txns = sync.to_feed_transactions(oper, tz)
    assert [(t.external_id, t.posted, t.amount) for t in txns] == [
        ("simplefin:TRN-OPER-1001", dt.date(2026, 10, 1), D("-86.41")),
        ("simplefin:TRN-OPER-1002", dt.date(2026, 10, 2), D("-129.95")),
        ("simplefin:TRN-OPER-1003", dt.date(2026, 10, 6), D("-12.00")),
    ]
    assert all(isinstance(t.amount, Decimal) for t in txns)
    assert txns[0].payee == "Puget Sound Energy" and txns[2].memo == "Business checking"
    bal = sync.to_feed_balance(oper, tz)
    assert (bal.as_of, bal.amount, bal.available) == (
        dt.date(2026, 10, 7),
        D("14382.17"),
        D("14382.17"),
    )


def test_start_date_filter():
    aset = FakeClient().accounts(fake_server.ACCESS_URL, dt.date(2026, 10, 3))
    oper = next(a for a in aset.accounts if a.id == "ACT-OPER-4321")
    assert [t.id for t in oper.transactions if not t.pending] == ["TRN-OPER-1003"]


def test_wrong_credentials_is_auth_error():
    with pytest.raises(sfc.AuthError):
        FakeClient().accounts(f"https://x:y@{fake_server.HOST}/simplefin")


def test_api_messages_and_extra_fields_parse():
    """Shapes seen on the beta-bridge demo: x-api-message, holdings, transacted_at."""
    aset = FakeClient().accounts(fake_server.ACCESS_URL)
    assert aset.messages == [fake_server.NO_START_MESSAGE] and aset.errors == []
    assert FakeClient().accounts(fake_server.ACCESS_URL, dt.date(2026, 9, 1)).messages == []
    data = {
        "errors": [],
        "x-api-message": ["note"],
        "accounts": [
            {
                "id": "Demo Checking",
                "name": "SimpleFIN Checking",
                "currency": "USD",
                "balance": "24668.87",
                "available-balance": "24668.87",
                "balance-date": 1791504000,
                "holdings": [{"id": "h1", "market_value": "105884.8"}],
                "org": {"domain": "d.example", "name": "Demo", "sfin-url": "https://x"},
                "transactions": [
                    {
                        "id": "1791446400",
                        "posted": 1791446400,
                        "amount": "-52.23",
                        "transacted_at": 1791446400,
                        "mcc": "5812",
                    }
                ],
            }
        ],
    }
    aset = sfc.parse_accounts(data)
    assert aset.messages == ["note"]
    assert aset.accounts[0].transactions[0].amount == D("-52.23")


def test_forbidden_json_body_is_auth_error():
    resp = fake_server.transport().handle_request(
        httpx.Request("GET", f"https://{fake_server.HOST}/simplefin/accounts")
    )
    assert resp.status_code == 403 and resp.json() == fake_server.FORBIDDEN


# ------------------------------------------------------------------ sync


def test_sync_imports_transactions_and_balance(connected, operating):
    result = sync.sync(connected, client=FakeClient())
    assert result.ok and result.imported == 3 and result.unmapped == 1
    assert BankTransaction.objects.filter(bank_account=operating).count() == 3
    batch = ImportBatch.objects.get()
    assert batch.source == "feed" and batch.provider == "SimpleFIN"
    bal = StatementBalance.objects.get(bank_account=operating)
    assert bal.amount == D("14382.17") and bal.as_of == dt.date(2026, 10, 7)
    operating.refresh_from_db()
    assert operating.last_synced_at == fake_server.SYNCED_AT
    conn = Connection.objects.get()
    assert conn.last_synced_at == fake_server.SYNCED_AT and conn.error_kind == ""
    assert not JournalEntry.objects.exists()  # never posts to the books


def test_second_sync_adds_no_duplicates_or_batches(connected, operating):
    sync.sync(connected, client=FakeClient())
    again = sync.sync(connected, client=FakeClient())
    # The 5-day overlap re-fetches Oct 2 and Oct 6; Oct 1 is outside it.
    assert again.ok and again.imported == 0 and again.duplicates == 2
    assert BankTransaction.objects.count() == 3
    assert ImportBatch.objects.count() == 1


def test_new_transactions_on_later_sync(connected, operating):
    sync.sync(connected, client=FakeClient())
    accounts = [dict(a) for a in fake_server.ACCOUNTS]
    accounts[0] = dict(accounts[0], balance="14370.17")
    accounts[0]["transactions"] = [
        *accounts[0]["transactions"],
        {
            "id": "TRN-OPER-1004",
            "posted": fake_server._unix(2026, 10, 7),
            "amount": "-12.00",
            "description": "WIRE FEE",
        },
    ]
    result = sync.sync(connected, client=FakeClient(accounts=accounts))
    assert result.imported == 1 and result.duplicates == 2
    assert BankTransaction.objects.count() == 4
    assert StatementBalance.objects.get().amount == D("14370.17")


@pytest.mark.parametrize(("state", "kind"), [("revoked", "auth"), ("payment", "payment")])
def test_errors_recorded_and_become_todos(connected, operating, state, kind):
    result = sync.sync(connected, client=FakeClient(state=state))
    assert not result.ok and result.error_kind == kind
    conn = Connection.objects.get()
    assert conn.error_kind == kind and conn.error_at is not None
    items = todos.todo_items(connected)
    assert items[0]["id"] == f"simplefin-{kind}"
    assert str(items[0]["title"]) == "Reconnect the bank feed"
    client = FakeClient()
    if kind == "auth":
        # Revoked: no more requests until the board reconnects.
        assert not sync.sync(connected, client=client).ok and client.requests == []
        assert sync.sync_due(timezone.now() + dt.timedelta(days=2)) == []
        sync.connect(connected, fake_server.SETUP_TOKEN, client=client)
    # A later good sync clears the error.
    assert sync.sync(connected, client=FakeClient()).ok
    assert not any(i["id"].startswith("simplefin-auth") for i in todos.todo_items(connected))


def test_network_error_is_recorded_not_raised(connected):
    def fail(request):
        raise httpx.ConnectError("down")

    client = sfc.SimpleFinClient(transport=httpx.MockTransport(fail))
    result = sync.sync(connected, client=client)
    assert not result.ok and result.error_kind == "network"
    assert not [i for i in todos.todo_items(connected) if i["id"] == "simplefin-network"]


def test_unmapped_and_ignored_todos(connected):
    reserve = SimpleFinAccount.objects.get(sf_id="ACT-RSRV-8765")
    assert [i["id"] for i in todos.todo_items(connected)] == [f"simplefin-map-{reserve.pk}"]
    sync.map_account(reserve, None, ignore=True)
    assert todos.todo_items(connected) == []


def test_sync_requires_connection(assoc):
    with pytest.raises(sync.NotConnected):
        sync.sync(assoc, client=FakeClient())


def test_fake_selected_by_setting_or_env(assoc, monkeypatch):
    plugins.save_installation(assoc, "simplefin", enabled=True, data={"use_fake": True})
    inst = plugins.installation_for(assoc, "simplefin")
    assert isinstance(sync.client_for(inst), FakeClient)
    inst.settings = {"use_fake": False}
    assert not isinstance(sync.client_for(inst), FakeClient)
    monkeypatch.setenv("HOMEOA_SIMPLEFIN_FAKE", "1")
    assert isinstance(sync.client_for(inst), FakeClient)


# ------------------------------------------------------------------ provider + job


def test_provider_resolves_and_fetches(connected):
    feed = providers.get_provider("BankFeedProvider", connected)
    assert isinstance(feed, SimpleFinFeed) and feed.syncs
    from homeoa.providers.bankfeed import FeedAccount

    txns = feed.fetch(
        FeedAccount(
            association_id=connected.pk, bank_account_id=0, external_account_id="ACT-RSRV-8765"
        ),
        dt.date(2026, 9, 1),
    )
    assert [(t.external_id, t.amount) for t in txns] == [("simplefin:TRN-RSRV-2001", D("34.96"))]
    status = feed.feed_status(connected)
    assert status["connected"] and status["manage_url"] == "/plugins/simplefin/"


def test_job_registered():
    from procrastinate.contrib.django import app

    from homeoa_simplefin import tasks

    assert tasks.TASK_NAME in app.tasks
    periodic = [p.periodic_id for p in app.periodic_registry.periodic_tasks.values()]
    assert tasks.TASK_NAME in periodic


def test_sync_due_once_a_day_at_random_minute(connected, operating, monkeypatch):
    monkeypatch.setenv("HOMEOA_SIMPLEFIN_FAKE", "1")
    day = dt.datetime(2026, 10, 9, tzinfo=fake_server.PACIFIC)
    Connection.objects.update(  # 9:41 am Pacific; last tried the day before
        sync_minute=9 * 60 + 41, last_attempt_at=day - dt.timedelta(days=1)
    )
    assert sync.sync_due(day.replace(hour=9, minute=40)) == []
    assert sync.sync_due(day.replace(hour=9, minute=45)) == [connected.pk]
    assert sync.sync_due(day.replace(hour=14)) == []  # once a day
    assert sync.sync_due((day + dt.timedelta(days=1)).replace(hour=9, minute=41)) == [connected.pk]


def test_random_sync_minute_spreads():
    from homeoa_simplefin.models import random_sync_minute

    values = {random_sync_minute() for _ in range(50)}
    assert len(values) > 1 and all(0 <= v < 1440 for v in values)


def test_daily_quota_refuses_past_24(connected, operating):
    client = FakeClient()
    Connection.objects.update(request_day=timezone.now().date(), request_count=24)
    conn = Connection.objects.get()
    assert conn.request_count == 24
    result = sync.sync(connected, client=client)
    assert not result.ok and result.error_kind == "quota" and client.requests == []
    assert "24 requests a day" in result.error
    # Single-account requests have their own quota.
    assert sync.spend_request(connected, sync.ONE_ACCOUNT) == 1
    # A new day resets it.
    tomorrow = timezone.now().astimezone(dt.UTC).date() + dt.timedelta(days=1)
    assert sync.spend_request(connected, today=tomorrow) == 1


def test_each_request_is_counted(connected, operating):
    before = Connection.objects.get().request_count
    sync.sync(connected, client=FakeClient())
    assert Connection.objects.get().request_count == before + 1


def test_windows_split_at_90_days():
    d = dt.date
    assert sync.windows(d(2026, 1, 1), d(2026, 2, 1)) == [(d(2026, 1, 1), d(2026, 2, 1))]
    spans = sync.windows(d(2026, 1, 1), d(2026, 10, 1))
    assert len(spans) == 4 and spans[0] == (d(2026, 1, 1), d(2026, 4, 1))
    assert all((b - a).days <= 90 for a, b in spans) and spans[-1][1] == d(2026, 10, 1)


def test_long_backfill_uses_windows_and_dedupes(connected, operating):
    client = FakeClient()
    aset = sync.fetch(
        connected, client, fake_server.ACCESS_URL, dt.date(2026, 3, 1), dt.date(2026, 10, 8)
    )
    assert len(client.requests) == 3
    for req in client.requests:
        start = int(req.url.params["start-date"])
        end = int(req.url.params["end-date"])
        assert 0 < end - start <= 90 * 86400
    oper = next(a for a in aset.accounts if a.id == "ACT-OPER-4321")
    ids = [t.id for t in oper.transactions]
    assert len(ids) == len(set(ids)) == 4  # pending one repeats per window, kept once


def test_errlist_and_warnings_shown(connected, operating):
    client = FakeClient(
        errlist=[{"code": "con.auth", "msg": "First Tech needs sign-in"}],
        warnings=["Balance may be stale"],
    )
    assert sync.sync(connected, client=client).ok
    conn = Connection.objects.get()
    assert conn.bank_errors == ["First Tech needs sign-in"]
    assert conn.bank_messages == ["Balance may be stale"]
    item = next(i for i in todos.todo_items(connected) if i["id"] == "simplefin-bank-errors")
    assert "First Tech needs sign-in" in item["sub"]


def test_provider_fetch_counts_single_account_quota(connected):
    feed = providers.get_provider("BankFeedProvider", connected)
    from homeoa.providers.bankfeed import FeedAccount

    feed.fetch(
        FeedAccount(
            association_id=connected.pk, bank_account_id=0, external_account_id="ACT-RSRV-8765"
        ),
        dt.date(2026, 9, 1),
    )
    assert Connection.objects.get().account_request_count == 1


def test_disabled_installation_not_synced(connected, monkeypatch):
    monkeypatch.setenv("HOMEOA_SIMPLEFIN_FAKE", "1")
    PluginInstallation.objects.update(enabled=False)
    assert sync.sync_due() == []


# ------------------------------------------------------------------ views


@pytest.fixture
def board(client, assoc):
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("treasurer@example.org")
    grant(user, association=assoc, title="treasurer")
    grant(user, "sys_admin", association=assoc)  # connecting the bank is the sys admin's
    client.force_login(user)
    return client


def test_connect_view_link_and_sync_now(board, assoc, operating, monkeypatch):
    monkeypatch.setenv("HOMEOA_SIMPLEFIN_FAKE", "1")
    resp = board.get("/plugins/simplefin/")
    assert resp.status_code == 200 and "Not connected." in resp.content.decode()
    resp = board.post(
        "/plugins/simplefin/connect/", {"setup_token": fake_server.SETUP_TOKEN}, follow=True
    )
    html = resp.content.decode()
    assert "Bank feed connected." in html
    assert html.count('data-testid="simplefin-account"') == 2
    oper = SimpleFinAccount.objects.get(sf_id="ACT-OPER-4321")
    rsrv = SimpleFinAccount.objects.get(sf_id="ACT-RSRV-8765")
    resp = board.post(
        "/plugins/simplefin/link/",
        {f"link-{oper.pk}": str(operating.pk), f"link-{rsrv.pk}": "ignore"},
        follow=True,
    )
    oper.refresh_from_db()
    rsrv.refresh_from_db()
    assert oper.bank_account == operating and rsrv.ignored
    resp = board.post("/money/bank/sync/", follow=True)
    html = resp.content.decode()
    assert "Bank sync imported 3 new transactions" in html
    assert html.count('data-testid="bank-txn"') == 3
    assert 'data-testid="bank-feed"' in html and "9:12 am" in html


def test_bad_token_view_message(board, assoc, monkeypatch):
    monkeypatch.setenv("HOMEOA_SIMPLEFIN_FAKE", "1")
    bad = base64.b64encode(f"https://{fake_server.HOST}/simplefin/claim/old".encode()).decode()
    resp = board.post("/plugins/simplefin/connect/", {"setup_token": bad}, follow=True)
    assert "Tokens work only once" in resp.content.decode()
    assert not plugins.is_enabled(assoc.pk, "simplefin")


def test_sync_without_feed_falls_back(board, assoc):
    resp = board.post("/money/bank/sync/", follow=True)
    assert "No bank feed is turned on" in resp.content.decode()


def test_read_only_board_cannot_sync(client, connected):
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("member@example.org")
    grant(user, association=connected)
    client.force_login(user)
    assert client.post("/money/bank/sync/").status_code == 403
    resp = client.get("/plugins/simplefin/")
    assert resp.status_code == 200 and "Sync now" not in resp.content.decode()


def test_same_transaction_id_in_two_accounts_imports_both(connected, operating):
    """The bridge's transaction ids are unique per account only."""
    reserve = BankAccount.objects.create(
        association=connected,
        name="Reserve savings",
        account_number="000987658765",
        gl_account=Account.objects.get(code="1010"),
        fund=Fund.objects.get(code="reserve"),
    )
    sync.map_account(SimpleFinAccount.objects.get(sf_id="ACT-RSRV-8765"), reserve)
    accounts = [dict(a) for a in fake_server.ACCOUNTS]
    shared = {"id": "1791446400", "posted": fake_server._unix(2026, 10, 5), "amount": "-1.00"}
    for a in accounts:
        a["transactions"] = [*a["transactions"], shared]
    result = sync.sync(connected, client=FakeClient(accounts=accounts))
    assert result.ok and result.imported == 6
    for bank in (operating, reserve):
        assert BankTransaction.objects.filter(
            bank_account=bank, external_id="simplefin:1791446400"
        ).exists()
