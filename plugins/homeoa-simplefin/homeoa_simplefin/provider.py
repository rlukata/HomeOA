"""``SimpleFinFeed``: the ``BankFeedProvider`` this plugin registers."""

from __future__ import annotations

import datetime as dt

from django.urls import reverse

from homeoa.providers.bankfeed import BankFeedProvider, FeedAccount, ParsedStatement
from homeoa.providers.defaults.fileupload import FileUploadFeed

from . import sync as svc


class SimpleFinFeed(BankFeedProvider):
    syncs = True
    name = "SimpleFIN"

    def __init__(self, installation=None):
        self.installation = installation
        self._files = FileUploadFeed()

    # File upload still works while the feed is on (fallback).
    def parse_file(self, filename, data):
        return self._files.parse_file(filename, data)

    def parse_statement(self, filename, data, **options):
        return self._files.parse_statement(filename, data, **options)

    def _access_url(self) -> str:
        url = (getattr(self.installation, "secrets", None) or {}).get("access_url")
        if not url:
            raise svc.NotConnected("SimpleFIN is not connected.")
        return url

    def fetch_statement(self, account: FeedAccount, since: dt.date) -> ParsedStatement:
        assoc = self.installation.association
        tz = svc._tz(assoc)
        client = svc.client_for(self.installation)
        today = client.now().astimezone(tz).date()
        aset = svc.fetch(
            assoc,
            client,
            self._access_url(),
            since,
            today + dt.timedelta(days=1),
            account_ids=[account.external_account_id],
        )
        txns, balances = [], []
        for a in aset.accounts:
            if a.id == account.external_account_id:
                txns += svc.to_feed_transactions(a, tz)
                balances.append(svc.to_feed_balance(a, tz))
        return ParsedStatement(transactions=txns, balances=balances)

    def fetch(self, account: FeedAccount, since: dt.date):
        return self.fetch_statement(account, since).transactions

    def sync(self, association, *, user=None):
        return svc.sync(association, user=user)

    def feed_status(self, association) -> dict:
        conn = svc.Connection.all_associations.filter(association=association).first()
        return {
            "name": self.name,
            "last_synced_at": conn.last_synced_at if conn else None,
            "error_kind": conn.error_kind if conn else "",
            "error": conn.get_error_kind_display() if conn and conn.error_kind else "",
            "manage_url": reverse("homeoa_simplefin:index"),
            "connected": bool(
                (getattr(self.installation, "secrets", None) or {}).get("access_url")
            ),
        }
