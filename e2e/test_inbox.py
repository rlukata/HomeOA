"""In-app notifications page and header bell (board)."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, manage
from helpers import m2
from helpers import seed as seed_helpers

_FAIL = """
from homeoa.core.models import Association
from homeoa.payments.autopay import reference_failed
c = reference_failed(Association.objects.get(), {intent!r}, "R01 insufficient funds")
print(c.status if c else None)
"""

_UNREAD = f"""
import json
from homeoa.notify.models import Delivery
qs = Delivery.all_associations.filter(
    channel="inapp", notification__recipient__user__email={BOARD_EMAIL!r},
    notification__type_key="payments.autopay_failed_board")
n = qs.latest("pk").notification
print("E2EJSON" + json.dumps({{"pk": n.pk}}))
"""


@pytest.fixture
def residents_on(seed):
    seed_helpers.billing()
    m2.residents_enabled(True)


def _bell_count(page) -> int:
    bell = page.get_by_test_id("notif-bell").first
    label = bell.get_attribute("aria-label") or ""
    m = re.search(r"(\d+) unread", label)
    return int(m.group(1)) if m else 0


def test_board_inbox_bell_and_mark_read(page, residents_on, login, axe):
    login(BOARD_EMAIL)
    page.goto("/")
    before = _bell_count(page)
    page.context.clear_cookies()

    # Trigger a board notification (autopay failure).
    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/")
    page.get_by_role("switch", name="Autopay").click()
    page.get_by_role("button", name="Turn on autopay").click()
    expect(page.get_by_test_id("autopay-line")).to_contain_text("Autopay is on from")
    page.context.clear_cookies()
    m2.run_scheduled("2026-10-26")
    m2.run_scheduled("2026-11-10")
    intent = m2.stripe_fake("last-intent")
    r = manage("shell", "-c", _FAIL.format(intent=intent["id"]), check=False)
    assert r.stdout.strip().endswith("failed"), r.stdout + r.stderr[-1500:]
    m2.drain_jobs()
    pk = seed_helpers.run_script(_UNREAD, "board notif")["pk"]

    login(BOARD_EMAIL)
    page.goto("/")
    assert _bell_count(page) == before + 1
    expect(page.get_by_test_id("notif-count").first).to_have_text(str(before + 1))
    page.get_by_test_id("notif-bell").first.click()
    expect(page).to_have_url(re.compile(r"/notifications/$"))
    row = page.get_by_test_id(f"notif-row-{pk}")
    expect(row).to_contain_text(re.compile(r"Autopay of \$[\d,.]+ for unit A failed"))
    expect(row).to_contain_text("New")
    axe(page)

    row.get_by_role("button", name=re.compile(r"^Mark .* as read$")).click()
    expect(page.get_by_test_id(f"notif-row-{pk}")).not_to_contain_text("New")
    expect(page.get_by_test_id(f"notif-row-{pk}").get_by_role("button")).to_have_count(0)
    assert _bell_count(page) == before
    axe(page)
