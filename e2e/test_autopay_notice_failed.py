"""Autopay history on the board and owner autopay pages; a failed
pre-debit notice skips the debit (skipped_notice_failed) and raises a board to-do and
alert (fake Stripe only)."""

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, manage
from helpers import m2
from helpers import seed as seed_helpers

BOARD_AUTOPAY = "/money/payments/autopay/settings/"
OWNER_AUTOPAY = "/me/pay/autopay/?unit=A"

# Run the scheduler with the pre-debit notice broken (as a dead channel would be).
_RUN_BROKEN = """
from django.core.management import call_command
import homeoa.payments.autopay as ap
def boom(*a, **k):
    raise RuntimeError("notice channel down")
ap._send_notice = boom
call_command("run_scheduled", "--as-of", AS_OF)
print("OK")
"""

_STATE = """
import json
from homeoa.notify.models import Delivery
from homeoa.payments.autopay import AutopayCharge
c = AutopayCharge.all_associations.filter(unit__number="A").order_by("pk").last()
alerts = Delivery.all_associations.filter(
    notification__type_key="payments.autopay_notice_failed_board")
print("E2EJSON" + json.dumps({
    "pk": c.pk, "status": c.status, "reference": c.reference,
    "board": sorted({d.notification.recipient.email or "" for d in alerts}),
}))
"""


@pytest.fixture
def residents_on(seed):
    seed_helpers.billing()
    m2.residents_enabled(True)


def _enroll(page, login):
    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/")
    page.get_by_role("switch", name="Autopay").click()
    page.get_by_role("button", name="Turn on autopay").click()
    expect(page.get_by_test_id("autopay-line")).to_contain_text("Autopay is on from")
    page.context.clear_cookies()


def test_autopay_history_board_and_owner(page, residents_on, login, axe):
    _enroll(page, login)
    m2.run_scheduled("2026-10-26")
    m2.run_scheduled("2026-11-10")
    pk = seed_helpers.run_script(_STATE, "state")["pk"]
    login(BOARD_EMAIL)
    page.goto(BOARD_AUTOPAY)
    row = page.get_by_test_id(f"autopay-charge-{pk}")
    expect(row).to_contain_text("A")
    expect(row).to_contain_text("November 2026")
    axe(page)
    page.context.clear_cookies()
    login(m2.OWNERA_EMAIL)
    page.goto(OWNER_AUTOPAY)
    expect(page.get_by_test_id("autopay-history")).to_be_visible()
    expect(page.get_by_test_id(f"autopay-charge-{pk}")).to_contain_text("November 2026")
    axe(page)


def test_failed_notice_skips_debit(page, residents_on, login, axe):
    _enroll(page, login)
    for day in ("2026-10-26", "2026-11-10"):
        r = manage("shell", "-c", f"AS_OF={day!r}\n" + _RUN_BROKEN, check=False)
        assert r.stdout.strip().endswith("OK"), r.stdout + r.stderr[-1500:]
    m2.drain_jobs()
    st = seed_helpers.run_script(_STATE, "state")
    assert st["status"] == "skipped_notice_failed", st
    assert st["reference"] == "", st  # no debit started
    assert BOARD_EMAIL in st["board"], st

    login(BOARD_EMAIL)
    page.goto("/")
    expect(page.get_by_text("Pre-debit notice failed for unit A").first).to_be_visible()
    page.goto(BOARD_AUTOPAY)
    row = page.get_by_test_id(f"autopay-charge-{st['pk']}")
    expect(row).to_contain_text("Not debited: notice failed")
    axe(page)
    page.context.clear_cookies()
    login(m2.OWNERA_EMAIL)
    page.goto(OWNER_AUTOPAY)
    expect(page.get_by_test_id(f"autopay-charge-{st['pk']}")).to_contain_text(
        "No late fee for this month"
    )


# Resend notice / collected manually from the autopay history.
_RESCHED = """
import json
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_today
from homeoa.payments.autopay import notice_days
from homeoa.payments.models import AutopayCharge
c = AutopayCharge.all_associations.get(pk=PK)
today = association_today(c.association)
print("E2EJSON" + json.dumps({
    "status": c.status, "debit_on": str(c.debit_on), "today": str(today),
    "least": notice_days(c.association, today), "reference": c.reference,
    "acked": c.acknowledged_at is not None,
    "audited": AuditEvent.all_associations.filter(action="payments.autopay_notice_resent").exists(),
}))
"""


def test_resend_notice_reschedules_debit(page, residents_on, login):
    import datetime as dt

    _enroll(page, login)
    for day in ("2026-10-26", "2026-11-10"):
        manage("shell", "-c", f"AS_OF={day!r}\n" + _RUN_BROKEN, check=False)
    m2.drain_jobs()
    st = seed_helpers.run_script(_STATE, "state")
    assert st["status"] == "skipped_notice_failed", st

    login(BOARD_EMAIL)
    page.goto(BOARD_AUTOPAY)
    page.get_by_test_id(f"autopay-charge-{st['pk']}").get_by_role(
        "button", name="Resend notice"
    ).click()
    r = seed_helpers.run_script(f"PK={st['pk']}\n" + _RESCHED, "resched")
    assert r["status"] == "scheduled" and r["reference"] == "", r
    earliest = dt.date.fromisoformat(r["today"]) + dt.timedelta(days=r["least"])
    assert dt.date.fromisoformat(r["debit_on"]) >= earliest, r
    assert r["audited"], r
    page.goto("/")
    expect(page.get_by_text("Pre-debit notice failed for unit A")).to_have_count(0)


def test_collected_manually_clears_todo(page, residents_on, login):
    _enroll(page, login)
    for day in ("2026-10-26", "2026-11-10"):
        manage("shell", "-c", f"AS_OF={day!r}\n" + _RUN_BROKEN, check=False)
    m2.drain_jobs()
    st = seed_helpers.run_script(_STATE, "state")
    assert st["status"] == "skipped_notice_failed", st

    login(BOARD_EMAIL)
    page.goto(BOARD_AUTOPAY)
    row = page.get_by_test_id(f"autopay-charge-{st['pk']}")
    row.get_by_role("button", name="Collected manually").click()
    expect(page.get_by_text("The failed autopay is off the to-do list")).to_be_visible()
    expect(row.get_by_role("button", name="Resend notice")).to_have_count(0)
    r = seed_helpers.run_script(f"PK={st['pk']}\n" + _RESCHED, "acked")
    assert r["acked"] and r["status"] == "skipped_notice_failed", r
    page.goto("/")
    expect(page.get_by_text("Pre-debit notice failed for unit A")).to_have_count(0)
