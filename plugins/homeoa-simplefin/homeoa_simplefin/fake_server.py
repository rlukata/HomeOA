"""A fake SimpleFIN Bridge: an ``httpx.MockTransport`` with fixed Cedar Row fixtures.

Used by tests, the demo seed and E2E (``use_fake`` setting or ``HOMEOA_SIMPLEFIN_FAKE=1``).
It never touches the network. ``FakeClient`` also pins the clock to ``SYNCED_AT`` so the
seeded UI reads "synced 9:12 am".

* Setup token ``SETUP_TOKEN`` claims ``ACCESS_URL``; any other claim path gets 403.
* ``GET /simplefin/accounts`` with the right basic auth returns ``ACCOUNTS`` filtered by
  ``start-date`` and ``account``; wrong credentials get 403.
* Shapes match the beta-bridge demo: JSON 403 bodies on ``/accounts``, ``x-api-message``
  when ``start-date`` is missing, ``holdings`` and ``transacted_at`` present. Real
  transaction ids are unique per account only; pass ``accounts=`` to test repeats.
* ``transport(state="revoked")`` answers 403 and ``state="payment"`` 402 on ``/accounts``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from zoneinfo import ZoneInfo

import httpx

from .client import SimpleFinClient

HOST = "bridge.simplefin.fake"
CLAIM_URL = f"https://{HOST}/simplefin/claim/demo"
SETUP_TOKEN = base64.b64encode(CLAIM_URL.encode()).decode()
USER, PASSWORD = "demo", "fake-secret"
ACCESS_URL = f"https://{USER}:{PASSWORD}@{HOST}/simplefin"

PACIFIC = ZoneInfo("America/Los_Angeles")
#: The fake's "now": Oct 7 2026, 9:12 am Pacific.
SYNCED_AT = dt.datetime(2026, 10, 7, 9, 12, tzinfo=PACIFIC)


def _unix(y, m, d, h=12) -> int:
    return int(dt.datetime(y, m, d, h, tzinfo=PACIFIC).timestamp())


ORG = {
    "domain": "firsttech.example",
    "name": "First Tech Federal Credit Union",
    "id": "firsttech.example",
    "url": "https://firsttech.example",
    "sfin-url": f"https://{HOST}/simplefin",
}
NO_START_MESSAGE = "Provide a 'start-date' parameter to receive transactions prior to yesterday"
FORBIDDEN = {"errors": ["Forbidden"], "accounts": []}

ACCOUNTS = [
    {
        "org": ORG,
        "id": "ACT-OPER-4321",
        "name": "Operating checking ··4321",
        "currency": "USD",
        "balance": "14382.17",
        "available-balance": "14382.17",
        "balance-date": int(SYNCED_AT.timestamp()),
        "transactions": [
            {
                "id": "TRN-OPER-1001",
                "posted": _unix(2026, 10, 1),
                "transacted_at": _unix(2026, 9, 30),
                "amount": "-86.41",
                "description": "PUGET SOUND ENERGY AUTOPAY",
                "payee": "Puget Sound Energy",
            },
            {
                "id": "TRN-OPER-1002",
                "posted": _unix(2026, 10, 2),
                "amount": "-129.95",
                "description": "COMCAST BUSINESS",
                "payee": "Comcast",
            },
            {
                "id": "TRN-OPER-1003",
                "posted": _unix(2026, 10, 6),
                "amount": "-12.00",
                "description": "MONTHLY SERVICE FEE",
                "memo": "Business checking",
            },
            {
                "id": "TRN-OPER-PEND",
                "posted": 0,
                "amount": "-45.00",
                "description": "PENDING CARD PURCHASE",
                "pending": True,
            },
        ],
    },
    {
        "org": ORG,
        "id": "ACT-RSRV-8765",
        "name": "Reserve savings ··8765",
        "currency": "USD",
        "balance": "41950.00",
        "balance-date": int(SYNCED_AT.timestamp()),
        "transactions": [
            {
                "id": "TRN-RSRV-2001",
                "posted": _unix(2026, 9, 30),
                "amount": "34.96",
                "description": "INTEREST PAYMENT",
            },
        ],
    },
]


def _accounts_response(request: httpx.Request, accounts: list[dict]) -> dict:
    start = request.url.params.get("start-date")
    end = request.url.params.get("end-date")
    wanted = request.url.params.get_list("account")
    out = []
    for a in accounts:
        if wanted and a["id"] not in wanted:
            continue
        a = dict(a)
        a.setdefault("holdings", [])
        a["transactions"] = [
            t
            for t in a["transactions"]
            if t.get("pending")
            or (
                (start is None or t["posted"] >= int(start))
                and (end is None or t["posted"] < int(end))
            )
        ]
        out.append(a)
    body = {"errors": [], "accounts": out}
    if start is None:
        body["x-api-message"] = [NO_START_MESSAGE]
    return body


def transport(
    state: str = "ok",
    accounts: list[dict] | None = None,
    *,
    errlist: list | None = None,
    warnings: list[str] | None = None,
    requests: list | None = None,
) -> httpx.MockTransport:
    """``requests``, if given, collects every request (to count calls and check params)."""
    accounts = ACCOUNTS if accounts is None else accounts
    expected_auth = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.host != HOST:
            return httpx.Response(404)
        path = request.url.path
        if request.method == "POST" and path.startswith("/simplefin/claim/"):
            if str(request.url) != CLAIM_URL:
                return httpx.Response(403, text="Forbidden (was it already claimed?)")
            return httpx.Response(200, text=ACCESS_URL)
        if request.method == "GET" and path == "/simplefin/accounts":
            if state == "revoked" or request.headers.get("authorization") != expected_auth:
                return httpx.Response(403, json=FORBIDDEN)
            if state == "payment":
                return httpx.Response(402, text="Payment Required")
            body = _accounts_response(request, accounts)
            if errlist:
                body["errlist"] = list(errlist)
            if warnings:
                body["errors"] = list(warnings)
            return httpx.Response(200, content=json.dumps(body).encode())
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class FakeClient(SimpleFinClient):
    def __init__(self, state: str = "ok", accounts: list[dict] | None = None, **kwargs):
        self.requests: list[httpx.Request] = []
        super().__init__(transport=transport(state, accounts, requests=self.requests, **kwargs))

    def now(self) -> dt.datetime:
        return SYNCED_AT
