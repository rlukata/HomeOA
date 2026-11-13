"""Claim and sync services. Bank data is written only through ``banking.services``.

``connect(association, setup_token, *, user=None)``
    Claim the setup token, store the access URL encrypted in the plugin installation,
    enable the plugin and discover accounts. Raises ``SimpleFinError``.

``sync(association, *, user=None, client=None) -> SyncResult``
    Fetch accounts since the last sync (minus a ~5-day overlap, so pending items that
    post later are caught; duplicates are dropped by transaction id), import new
    transactions and balances of mapped accounts, and record the outcome on
    ``Connection``. Errors are recorded, never raised.

``sync_due(now=None)``
    Sync every enabled association once a day at its random ``sync_minute`` (the job).

The bridge's developer rules are enforced here, for the job, "Sync now" and the
provider alike: at most ``DAILY_LIMIT`` requests a day per access URL (all-accounts and
``?account=`` requests counted separately), at most ``WINDOW_DAYS`` per request (longer
ranges are split), and a revoked access URL stops all requests until the board reconnects.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from homeoa import audit
from homeoa.banking import services as banking
from homeoa.banking.models import BankAccount, BankTransaction, ImportSource, StatementBalance
from homeoa.core.context import association_context
from homeoa.plugins import services as plugins
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.providers.bankfeed import StatementBalance as FeedBalance

from .client import (
    AccountSet,
    AuthError,
    QuotaExceeded,
    SFAccount,
    SimpleFinClient,
    SimpleFinError,
    decode_setup_token,
)
from .models import Connection, ErrorKind, SimpleFinAccount

logger = logging.getLogger(__name__)

PLUGIN_ID = "simplefin"
PROVIDER_NAME = "SimpleFIN"
OVERLAP = dt.timedelta(days=5)
FIRST_SYNC_DAYS = 60
DAILY_LIMIT = 24
WINDOW_DAYS = 90
ALL_ACCOUNTS, ONE_ACCOUNT = "all", "account"
EXTERNAL_PREFIX = "simplefin:"


class NotConnected(Exception):
    pass


@dataclasses.dataclass
class SyncResult:
    ok: bool
    imported: int = 0
    duplicates: int = 0
    batches: list = dataclasses.field(default_factory=list)
    unmapped: int = 0
    error_kind: str = ""
    error: str = ""


def _tz(association) -> ZoneInfo:
    try:
        return ZoneInfo(association.timezone)
    except Exception:
        return ZoneInfo("UTC")


def demo_host_allowed(url: str | None) -> bool:
    """Whether ``url``'s host is on ``HOMEOA_SIMPLEFIN_DEMO_HOSTS``."""
    from django.conf import settings

    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and host in getattr(settings, "HOMEOA_SIMPLEFIN_DEMO_HOSTS", [])


def use_fake(installation, url: str | None = None, *, association=None) -> bool:
    """Fake bridge or real one. ``url`` is the claim or access URL about to be used
    (default: the installation's stored access URL).

    Sandbox associations use the fake bridge (OPS-05) unless
    ``HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE`` is on and the URL's host is a SimpleFIN demo
    host. ``HOMEOA_SIMPLEFIN_FAKE=1`` always wins.
    """
    from django.conf import settings

    if os.environ.get("HOMEOA_SIMPLEFIN_FAKE") == "1":
        return True
    if installation is not None:
        association = installation.association
    if association is not None:
        from homeoa.core.sandbox import is_sandbox

        if is_sandbox(association):
            if url is None and installation is not None:
                url = (installation.secrets or {}).get("access_url")
            if not getattr(settings, "HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE", False):
                return True  # sandbox associations never reach a real bank (OPS-05)
            if not demo_host_allowed(url):
                if url:
                    logger.warning(
                        "simplefin: sandbox refused non-demo bridge host %s; using fake",
                        urlsplit(url).hostname,
                    )
                return True
            return False
    return bool(installation and (installation.settings or {}).get("use_fake"))


def client_for(installation, url: str | None = None, *, association=None) -> SimpleFinClient:
    if use_fake(installation, url, association=association):
        from .fake_server import FakeClient

        return FakeClient()
    return SimpleFinClient()


def connection_for(association) -> Connection:
    conn, _ = Connection.all_associations.get_or_create(association=association)
    return conn


def to_feed_transactions(account: SFAccount, tz: ZoneInfo) -> list[FeedTxn]:
    """Posted (not pending) transactions as ``BankTransaction`` dataclasses."""
    return [
        FeedTxn(
            external_id=f"{EXTERNAL_PREFIX}{t.id}",
            posted=t.posted.astimezone(tz).date(),
            amount=t.amount,
            description=t.description,
            payee=t.payee,
            memo=t.memo,
        )
        for t in account.transactions
        if not t.pending
    ]


def to_feed_balance(account: SFAccount, tz: ZoneInfo) -> FeedBalance:
    return FeedBalance(
        as_of=account.balance_date.astimezone(tz).date(),
        amount=account.balance,
        available=account.available,
    )


def windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """``[start, end)`` split into spans of at most ``WINDOW_DAYS`` days."""
    out = []
    while start < end:
        stop = min(start + dt.timedelta(days=WINDOW_DAYS), end)
        out.append((start, stop))
        start = stop
    return out or [(end, end + dt.timedelta(days=1))]


def spend_request(association, quota: str = ALL_ACCOUNTS, today: dt.date | None = None) -> int:
    """Count one bridge request against today's quota; raise ``QuotaExceeded`` past it.

    The day is the UTC date. Returns the count after this request.
    """
    today = today or timezone.now().astimezone(dt.UTC).date()
    with transaction.atomic():
        conn = Connection.all_associations.select_for_update().get(
            pk=connection_for(association).pk
        )
        if conn.request_day != today:
            conn.request_day, conn.request_count, conn.account_request_count = today, 0, 0
        field = "request_count" if quota == ALL_ACCOUNTS else "account_request_count"
        used = getattr(conn, field)
        if used >= DAILY_LIMIT:
            raise QuotaExceeded(
                f"SimpleFIN allows {DAILY_LIMIT} requests a day; today's are used up. "
                "The next sync tomorrow will catch up."
            )
        setattr(conn, field, used + 1)
        conn.save(update_fields=["request_day", "request_count", "account_request_count"])
        return used + 1


def is_revoked(association) -> bool:
    conn = Connection.all_associations.filter(association=association).first()
    return bool(conn and conn.error_kind == ErrorKind.AUTH)


def fetch(association, client, access_url, start: dt.date, end: dt.date, *, account_ids=None):
    """One request per window of at most ``WINDOW_DAYS``, merged by account.

    Transactions repeated across windows are dropped by id; the balance comes from the
    latest window. Each request is counted against the daily quota first.
    """
    if is_revoked(association):
        raise AuthError("access URL revoked or disabled; reconnect with a new setup token")
    quota = ONE_ACCOUNT if account_ids else ALL_ACCOUNTS
    merged: dict[str, SFAccount] = {}
    errors, messages = [], []
    for w_start, w_end in windows(start, end):
        spend_request(association, quota)
        aset = client.accounts(access_url, w_start, end=w_end, account_ids=account_ids)
        errors += [e for e in aset.errors if e not in errors]
        messages += [m for m in aset.messages if m not in messages]
        for a in aset.accounts:
            prev = merged.get(a.id)
            if prev is None:
                merged[a.id] = a
                continue
            seen = {t.id for t in prev.transactions}
            txns = prev.transactions + [t for t in a.transactions if t.id not in seen]
            merged[a.id] = dataclasses.replace(a, transactions=txns)
    return AccountSet(accounts=list(merged.values()), errors=errors, messages=messages)


def connect(association, setup_token: str, *, user=None, client=None) -> SyncResult:
    inst = plugins.installation_for(association, PLUGIN_ID)
    if client is None:
        try:
            claim_url = decode_setup_token(setup_token)
        except SimpleFinError:
            claim_url = None
        client = client_for(inst, claim_url, association=association)
    access_url = client.claim(setup_token)
    data = dict(inst.settings) if inst else {}
    data["access_url"] = access_url
    plugins.save_installation(association, PLUGIN_ID, enabled=True, data=data)
    # A new access URL: fresh quotas and no stale "reconnect" state.
    Connection.all_associations.filter(pk=connection_for(association).pk).update(
        error_kind="",
        error_message="",
        error_at=None,
        request_day=None,
        request_count=0,
        account_request_count=0,
    )
    audit.record("simplefin.connect", connection_for(association), actor=user, changes={})
    return sync(association, user=user, client=client)


def _upsert_accounts(association, aset: AccountSet, tz) -> list[SimpleFinAccount]:
    out = []
    for a in aset.accounts:
        obj, _ = SimpleFinAccount.all_associations.update_or_create(
            association=association,
            sf_id=a.id,
            defaults={
                "name": a.name[:255],
                "org_name": a.org_name[:255],
                "currency": a.currency[:10],
                "balance": a.balance,
                "balance_date": a.balance_date.astimezone(tz).date(),
            },
        )
        out.append(obj)
    return out


def _import_account(link: SimpleFinAccount, account: SFAccount, tz, user, now, result):
    bank_account = link.bank_account
    txns = to_feed_transactions(account, tz)
    balance = to_feed_balance(account, tz)
    known = set(
        BankTransaction.all_associations.filter(
            bank_account=bank_account, external_id__in=[t.external_id for t in txns]
        ).values_list("external_id", flat=True)
    )
    new = [t for t in txns if t.external_id not in known]
    stored = StatementBalance.all_associations.filter(
        bank_account=bank_account, as_of=balance.as_of
    ).first()
    balance_changed = stored is None or (stored.amount, stored.available) != (
        balance.amount,
        balance.available,
    )
    if new or balance_changed:
        # Nothing new would just add an empty batch every few hours.
        batch = banking.import_transactions(
            bank_account,
            txns,
            balances=[balance],
            source=ImportSource.FEED,
            filename=f"{PROVIDER_NAME} {account.name}",
            provider=PROVIDER_NAME,
            user=user,
        )
        result.batches.append(batch)
        result.imported += batch.imported_count
        result.duplicates += batch.duplicate_count
    else:
        result.duplicates += len(txns)
    BankAccount.all_associations.filter(pk=bank_account.pk).update(last_synced_at=now)


def _record_error(association, exc: SimpleFinError, now) -> SyncResult:
    conn = connection_for(association)
    conn.last_attempt_at = now
    conn.error_kind = exc.kind if exc.kind in ErrorKind.values and exc.kind else "other"
    conn.error_message = str(exc)[:500]
    conn.error_at = now
    conn.save()
    logger.warning("SimpleFIN sync failed for association %s: %s", association.pk, exc)
    audit.record("simplefin.sync_error", conn, changes={"kind": conn.error_kind, "error": str(exc)})
    return SyncResult(ok=False, error_kind=conn.error_kind, error=conn.error_message)


def _start_date(association, today: dt.date, tz) -> dt.date:
    """Overlap the oldest linked account's last sync by ``OVERLAP``; a linked account that
    never synced (or a first sync) goes back ``FIRST_SYNC_DAYS``."""
    first = today - dt.timedelta(days=FIRST_SYNC_DAYS)
    synced = [
        link.bank_account.last_synced_at
        for link in SimpleFinAccount.all_associations.filter(
            association=association, bank_account__isnull=False, bank_account__active=True
        ).select_related("bank_account")
    ]
    if not synced or None in synced:
        return first
    return max(first, min(synced).astimezone(tz).date() - OVERLAP)


def sync(association, *, user=None, client=None, attempted_at=None) -> SyncResult:
    inst = plugins.installation_for(association, PLUGIN_ID)
    access_url = (inst.secrets or {}).get("access_url") if inst and inst.enabled else None
    if not access_url:
        raise NotConnected(association.pk)
    client = client or client_for(inst)
    now = client.now()
    attempted_at = attempted_at or timezone.now()
    tz = _tz(association)
    conn = connection_for(association)
    if conn.error_kind == ErrorKind.AUTH:
        # Revoked or disabled: never call the bridge again until the board reconnects.
        return SyncResult(ok=False, error_kind=conn.error_kind, error=conn.error_message)
    today = now.astimezone(tz).date()
    start = _start_date(association, today, tz)
    try:
        aset = fetch(association, client, access_url, start, today + dt.timedelta(days=1))
    except SimpleFinError as exc:
        return _record_error(association, exc, attempted_at)
    conn.refresh_from_db()
    result = SyncResult(ok=True)
    with transaction.atomic(), association_context(association):
        links = {link.sf_id: link for link in _upsert_accounts(association, aset, tz)}
        for account in aset.accounts:
            link = links[account.id]
            if link.bank_account_id is None or not link.bank_account.active:
                result.unmapped += 1
                continue
            _import_account(link, account, tz, user, now, result)
        conn.last_synced_at = now
        conn.last_attempt_at = attempted_at
        conn.error_kind = conn.error_message = ""
        conn.error_at = None
        conn.bank_errors = aset.errors
        conn.bank_messages = aset.messages
        conn.save()
    return result


def map_account(
    link: SimpleFinAccount, bank_account: BankAccount | None, *, ignore=False, user=None
):
    """Link ``link`` to ``bank_account``; ``None`` unlinks (``ignore`` marks it skipped)."""
    old = link.bank_account
    if bank_account is not None and bank_account.association_id != link.association_id:
        raise ValueError("bank account belongs to another association")
    link.bank_account = bank_account
    link.ignored = bank_account is None and ignore
    link.save(update_fields=["bank_account", "ignored"])
    audit.record(
        "simplefin.map",
        link,
        actor=user,
        changes={
            "bank_account": [str(old or ""), str(bank_account or "")],
            "ignored": link.ignored,
        },
    )


def is_due(conn: Connection | None, now: dt.datetime, tz) -> bool:
    """Once a local day, at or after the connection's random ``sync_minute``."""
    if conn is None:
        return True
    if conn.error_kind == ErrorKind.AUTH:
        return False
    local = now.astimezone(tz)
    if local.hour * 60 + local.minute < conn.sync_minute:
        return False
    return conn.last_attempt_at is None or conn.last_attempt_at.astimezone(tz).date() < local.date()


def sync_due(now: dt.datetime | None = None) -> list[int]:
    """Sync associations whose daily slot has come; returns their ids."""
    from homeoa.plugins.models import PluginInstallation

    now = now or timezone.now()
    synced = []
    for inst in PluginInstallation.all_associations.filter(
        plugin_id=PLUGIN_ID, enabled=True
    ).select_related("association"):
        if not (inst.secrets or {}).get("access_url"):
            continue
        conn = Connection.all_associations.filter(association=inst.association).first()
        if not is_due(conn, now, _tz(inst.association)):
            continue
        try:
            with audit.audit_context(label="job:homeoa_simplefin.sync_due"):
                sync(inst.association, attempted_at=now)
        except Exception:
            logger.exception("SimpleFIN sync crashed for association %s", inst.association_id)
            continue
        synced.append(inst.association_id)
    return synced
