"""Owner-portal test hooks (see e2e/README.md). All go through manage.py so the
tests never import Django. Command names are proposals until the tasks land."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from conftest import manage

OWNERC_EMAIL = "ownerc@cedarrow.example"  # Priya, owner of unit C
OWNERA_EMAIL = "ownera@cedarrow.example"  # Owner of unit A


def _run(*args: str) -> str:
    r = manage(*args, check=False)
    if r.returncode:
        pytest.fail(f"manage {' '.join(args)} failed: {r.stderr[-1500:]}")
    return r.stdout


def residents_enabled(on: bool) -> None:
    """Flip the association 'residents enabled' switch."""
    _run("e2e_residents", "on" if on else "off")


def run_scheduled(as_of: str) -> str:
    """Run due jobs for a date (dues, autopay, delinquency ladder)."""
    return _run("run_scheduled", "--as-of", as_of)


def stripe_fake(*args: str) -> dict:
    """Drive the fake provider; prints one JSON line."""
    out = _run("stripe_fake", *args)
    return json.loads(out.strip().splitlines()[-1])


def post_webhook(base_url: str, association_id: int, body: str, signature: str) -> int:
    """POST to /hooks/stripe/<association_id>/ ; returns the status."""
    req = urllib.request.Request(
        f"{base_url}/hooks/stripe/{association_id}/",
        data=body.encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Stripe-Signature": signature},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def email_bounce(to: str) -> None:
    """Mark the latest message to `to` bounced via the real handler."""
    _run("e2e_bounce", to)


def drain_jobs() -> None:
    """Run every queued background job once (the E2E server has no worker), e.g. emails."""
    _run("procrastinate", "worker", "--one-shot")
