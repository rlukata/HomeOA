"""The SimpleFIN Bridge protocol (https://www.simplefin.org/protocol.html) over httpx.

1. The board gets a *setup token* from the bridge: base64 of a one-time claim URL.
2. ``POST`` to the claim URL returns the *access URL* (``https://user:pass@host/path``).
3. ``GET {access_url}/accounts?start-date=<unix>`` returns accounts with balances and
   transactions. Amounts are decimal strings; dates are unix timestamps.

403 means the access URL was revoked (or the setup token already claimed); 402 means the
bridge subscription needs payment. Error bodies are JSON (``{"errors": [...], "accounts": []}``)
on ``/accounts`` and plain text on claim; only the status code is used. Transaction ids are
unique per account only, not across accounts. Extra fields (``transacted_at``, ``holdings``,
``org.sfin-url`` ...) are ignored. ``errlist`` holds errors (strings or ``{"msg": ...}``);
``errors`` holds warnings. Checked against the beta-bridge demo (live smoke test).
No Django imports here.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import logging
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

import httpx


class SimpleFinError(Exception):
    kind = "other"


class AuthError(SimpleFinError):
    kind = "auth"


class PaymentRequired(SimpleFinError):
    kind = "payment"


class NetworkError(SimpleFinError):
    kind = "network"


class ClaimError(SimpleFinError):
    kind = "claim"


class QuotaExceeded(SimpleFinError):
    """Refused locally: the daily request budget for this access URL is spent."""

    kind = "quota"


@dataclasses.dataclass(frozen=True, kw_only=True)
class SFTransaction:
    id: str
    posted: dt.datetime
    amount: Decimal
    description: str = ""
    payee: str = ""
    memo: str = ""
    pending: bool = False


@dataclasses.dataclass(frozen=True, kw_only=True)
class SFAccount:
    id: str
    name: str
    org_name: str
    currency: str
    balance: Decimal
    available: Decimal | None
    balance_date: dt.datetime
    transactions: list[SFTransaction]


@dataclasses.dataclass(frozen=True, kw_only=True)
class AccountSet:
    #: ``errlist``: problems the board must act on (e.g. a bank needs re-authentication).
    errors: list[str]
    accounts: list[SFAccount]
    #: Warnings: the ``errors`` strings and ``x-api-message`` notes from the bridge.
    messages: list[str] = dataclasses.field(default_factory=list)


def decode_setup_token(token: str) -> str:
    """The claim URL inside a setup token. Raises ``ClaimError`` if it isn't one."""
    raw = "".join((token or "").split())
    try:
        url = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ClaimError("not a setup token") from exc
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ClaimError("not a setup token")
    return url


def _split_access_url(access_url: str) -> tuple[str, httpx.BasicAuth | None]:
    parts = urlsplit(access_url)
    if parts.scheme != "https" or not parts.hostname:
        raise AuthError("invalid access URL")
    auth = None
    if parts.username is not None:
        auth = httpx.BasicAuth(parts.username, parts.password or "")
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", "")), auth


def _ts(value) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(value), tz=dt.UTC)


def _money(value) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise SimpleFinError(f"bad amount {value!r}") from exc


def _check(resp: httpx.Response) -> None:
    if resp.status_code == 403:
        raise AuthError("access denied (403)")
    if resp.status_code == 402:
        raise PaymentRequired("payment required (402)")
    if resp.status_code >= 400:
        raise SimpleFinError(f"HTTP {resp.status_code}")


def parse_accounts(data: dict) -> AccountSet:
    try:
        accounts = []
        for a in data.get("accounts") or []:
            org = a.get("org") or {}
            txns = [
                SFTransaction(
                    id=str(t["id"]),
                    posted=_ts(t["posted"]),
                    amount=_money(t["amount"]),
                    description=str(t.get("description") or ""),
                    payee=str(t.get("payee") or ""),
                    memo=str(t.get("memo") or ""),
                    pending=bool(t.get("pending", False)),
                )
                for t in a.get("transactions") or []
            ]
            avail = a.get("available-balance")
            accounts.append(
                SFAccount(
                    id=str(a["id"]),
                    name=str(a.get("name") or ""),
                    org_name=str(org.get("name") or org.get("domain") or ""),
                    currency=str(a.get("currency") or ""),
                    balance=_money(a["balance"]),
                    available=_money(avail) if avail not in (None, "") else None,
                    balance_date=_ts(a["balance-date"]),
                    transactions=txns,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise SimpleFinError(f"unexpected response: {exc}") from exc
    return AccountSet(
        accounts=accounts,
        errors=[_err_text(e) for e in data.get("errlist") or []],
        messages=[str(m) for m in (data.get("errors") or []) + (data.get("x-api-message") or [])],
    )


def _err_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("msg") or item.get("message") or item.get("code") or item)
    return str(item)


class SimpleFinClient:
    """``transport`` lets tests and the demo use ``fake_server.transport()``."""

    def __init__(self, transport: httpx.BaseTransport | None = None, timeout: float = 30.0):
        self.transport = transport
        self.timeout = timeout

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def _http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=self.timeout)

    def claim(self, setup_token: str) -> str:
        url = decode_setup_token(setup_token)
        # The claim URL is the setup token decoded: keep httpx's INFO request log (which
        # prints the URL) and the exception text (which may) out of the logs.
        httpx_log = logging.getLogger("httpx")
        level = httpx_log.level
        httpx_log.setLevel(max(level, logging.WARNING))
        try:
            with self._http() as http:
                resp = http.post(url, headers={"Content-Length": "0"})
        except httpx.HTTPError as exc:
            raise NetworkError(f"claim request failed ({type(exc).__name__})") from None
        finally:
            httpx_log.setLevel(level)
        if resp.status_code == 403:
            raise ClaimError("token already used or invalid (403)")
        _check(resp)
        access_url = resp.text.strip()
        _split_access_url(access_url)  # validate shape
        return access_url

    def accounts(
        self,
        access_url: str,
        start: dt.date | None = None,
        *,
        end: dt.date | None = None,
        account_ids: list[str] | None = None,
    ) -> AccountSet:
        base, auth = _split_access_url(access_url)
        params: list[tuple[str, str]] = []
        if start is not None:
            start_dt = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
            params.append(("start-date", str(int(start_dt.timestamp()))))
        if end is not None:
            end_dt = dt.datetime.combine(end, dt.time.min, tzinfo=dt.UTC)
            params.append(("end-date", str(int(end_dt.timestamp()))))
        for aid in account_ids or []:
            params.append(("account", aid))
        try:
            with self._http() as http:
                resp = http.get(f"{base}/accounts", params=params, auth=auth)
        except httpx.HTTPError as exc:
            raise NetworkError(str(exc)) from exc
        _check(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise SimpleFinError("response is not JSON") from exc
        return parse_accounts(data)
