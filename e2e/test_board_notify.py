"""Board-audience alerts reach a board login that has no linked registry Person."""

import re

import pytest
from playwright.sync_api import expect

from conftest import manage
from helpers import m2
from helpers import seed as seed_helpers

NOPERSON_EMAIL = "noperson-board@cedarrow.example"

_SETUP = f"""
from django.contrib.auth import get_user_model
from homeoa.registry.models import Person
u, _ = get_user_model().objects.get_or_create(email={NOPERSON_EMAIL!r})
u.is_active = True
u.save()
Person.all_associations.filter(user=u).delete()
print(Person.all_associations.filter(user=u).count())
"""


@pytest.fixture
def residents_on(seed):
    seed_helpers.billing()
    m2.residents_enabled(True)


def test_autopay_failed_alert_reaches_board_user_without_person(page, residents_on, login):
    r = manage("shell", "-c", _SETUP, check=False)
    assert r.returncode == 0 and r.stdout.strip().endswith("0"), r.stderr[-1500:]
    manage("grant_role", NOPERSON_EMAIL, "board", "--title", "secretary")

    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/")
    page.get_by_role("switch", name="Autopay").click()
    page.get_by_role("button", name="Turn on autopay").click()
    expect(page.get_by_test_id("autopay-line")).to_contain_text("Autopay is on from")

    m2.run_scheduled("2026-10-26")
    m2.run_scheduled("2026-11-10")
    intent = m2.stripe_fake("last-intent")
    # No caller wires the ACH-failed webhook to autopay.reference_failed yet (reported),
    # so drive the service hook directly.
    r = manage(
        "shell",
        "-c",
        f"""
from homeoa.core.models import Association
from homeoa.payments.autopay import reference_failed
c = reference_failed(Association.objects.get(), {intent["id"]!r}, "R01 insufficient funds")
print(c.status if c else None)
""",
        check=False,
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("failed"), r.stdout + r.stderr[-1500:]
    # Board alerts are delivered in-app only and there is no inbox page yet, so check
    # the delivered notification row for the no-Person board login.
    r = manage(
        "shell",
        "-c",
        f"""
from homeoa.notify.models import Delivery
for d in Delivery.all_associations.filter(
    notification__type_key="payments.autopay_failed_board",
    notification__recipient__user__email={NOPERSON_EMAIL!r},
):
    print("ROW", d.channel, d.status, d.notification.subject)
""",
        check=False,
    )
    rows = [ln for ln in r.stdout.splitlines() if ln.startswith("ROW")]
    assert len(rows) == 1, r.stdout + r.stderr[-1500:]
    assert re.match(r"ROW inapp delivered Autopay of \$[\d,.]+ for unit A failed", rows[0]), rows
