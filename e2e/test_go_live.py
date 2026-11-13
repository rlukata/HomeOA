"""Settings > Go live: the System admin's button makes a setting-up association live today.
Settings > Law changes: the yearly law check's review list and Mark reviewed.
Clock fixed at 2026-10-07."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, manage
from helpers.seed import run_script

NOT_LIVE = """
import json
from homeoa.core import status
from homeoa.core.context import association_context
from homeoa.core.models import Association
a = Association.objects.order_by("pk").first()
with association_context(a):
    status.set_go_live_date(a, None, reason="E2E: still setting up")
print("E2EJSON" + json.dumps({}))
"""


def test_the_system_admin_goes_live_from_settings(page, seed, login, axe):
    run_script(NOT_LIVE, "not live")
    login(BOARD_EMAIL)
    page.goto("/settings/")
    page.get_by_role("link", name="Go live", exact=True).click()
    expect(page.get_by_test_id("status-label")).to_have_text("Setting up")
    axe(page)
    form = page.get_by_test_id("go-live-form")
    form.get_by_role("checkbox").check()
    form.get_by_role("button", name="Go live").click()
    expect(page.get_by_text("The association is live from today.")).to_be_visible()
    expect(page.get_by_test_id("status-label")).to_have_text("Live since Oct 7, 2026")
    expect(page.get_by_test_id("go-live-form")).to_have_count(0)


def test_the_law_check_review_list_is_marked_reviewed(page, seed, login, axe):
    manage("law_check", "--fake", "--save")
    login(BOARD_EMAIL)
    page.goto("/settings/")
    page.get_by_test_id("settings-law-changes").click()
    expect(page.get_by_test_id("law-check-status")).to_have_text("Changes found")
    expect(page.get_by_test_id("law-review-list")).to_contain_text("SAMPLE 0000")
    expect(page.get_by_test_id("law-review-list")).to_contain_text(re.compile(r"RCW \d"))
    axe(page)
    page.get_by_test_id("law-mark-reviewed").click()
    expect(page.get_by_text("Marked reviewed.")).to_be_visible()
    expect(page.get_by_test_id("law-check-reviewed")).to_contain_text("Reviewed on")
    expect(page.get_by_test_id("law-mark-reviewed")).to_have_count(0)
