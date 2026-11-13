"""An autopay ACH failure reported later by webhook marks the charge failed and
alerts the owner and the board, once per event (fake Stripe only)."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, manage
from helpers import m2
from helpers import seed as seed_helpers

_STATE = """
import json
from homeoa.notify.models import Delivery
from homeoa.payments.autopay import AutopayCharge
c = AutopayCharge.all_associations.filter(unit__number="A").exclude(reference="").get()
rows = Delivery.all_associations.filter(notification__object_key=f"payments.autopaycharge:{c.pk}")
print("E2EJSON" + json.dumps({
    "status": c.status,
    "board": sorted(d.notification.recipient.email or "" for d in rows
                    if d.notification.type_key.endswith("_board")),
    "owner": sum(1 for d in rows if d.notification.type_key == "payments.autopay_failed"),
}))
"""


@pytest.fixture
def residents_on(seed):
    seed_helpers.billing()
    m2.residents_enabled(True)


def _enroll_and_debit(page, login):
    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/")
    page.get_by_role("switch", name="Autopay").click()
    page.get_by_role("button", name="Turn on autopay").click()
    expect(page.get_by_test_id("autopay-line")).to_contain_text("Autopay is on from")
    page.context.clear_cookies()
    m2.run_scheduled("2026-10-26")
    m2.run_scheduled("2026-11-10")
    return m2.stripe_fake("last-intent")


def _fail(base_url, intent_id):
    ev = m2.stripe_fake("webhook", "payment_intent.payment_failed", intent_id)
    status = m2.post_webhook(base_url, ev["association_id"], ev["body"], ev["signature"])
    assert status == 200, status
    return ev


def _ledger_entries() -> int:
    r = manage(
        "shell",
        "-c",
        "from homeoa.ledger.models import JournalEntry;"
        "print('N', JournalEntry.all_associations.count())",
    )
    return int(r.stdout.split("N")[-1])


def test_payment_failed_webhook_marks_autopay_failed(page, residents_on, login, base_url):
    intent = _enroll_and_debit(page, login)
    _fail(base_url, intent["id"])
    m2.drain_jobs()
    st = seed_helpers.run_script(_STATE, "autopay state")
    assert st["status"] == "failed", st
    assert BOARD_EMAIL in st["board"], st  # via board_recipients
    assert st["owner"] >= 1, st
    # Board to-do on Home (the owner gets the notice; the to-do is the board's).
    login(BOARD_EMAIL)
    page.goto("/")
    todo = page.get_by_text(re.compile(r"Autopay of \$[\d,.]+ from unit A failed")).first
    expect(todo).to_be_visible()


def test_duplicate_failed_webhook_is_idempotent(page, residents_on, login, base_url):
    intent = _enroll_and_debit(page, login)
    ev = _fail(base_url, intent["id"])
    first = seed_helpers.run_script(_STATE, "autopay state")
    entries = _ledger_entries()
    # Same event redelivered: no new notifications or postings.
    assert m2.post_webhook(base_url, ev["association_id"], ev["body"], ev["signature"]) == 200
    assert seed_helpers.run_script(_STATE, "autopay state") == first
    assert _ledger_entries() == entries
    # A later charge.failed for the same intent does not double-post either.
    later = m2.stripe_fake("webhook", "charge.failed", intent["id"])
    m2.post_webhook(base_url, later["association_id"], later["body"], later["signature"])
    assert seed_helpers.run_script(_STATE, "autopay state") == first
    assert _ledger_entries() == entries
