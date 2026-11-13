"""axe scans of the billing pages (/money/assessments/)."""

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import seed as seed_helpers


@pytest.fixture
def billing_seed(seed):
    return seed_helpers.billing()


@pytest.mark.parametrize("kind", ["index", "run", "run_detail", "schedule_new", "schedule_edit"])
def test_billing_page_axe(page, billing_seed, login, axe, kind):
    path = {
        "index": "/money/assessments/",
        "run": f"/money/assessments/run/?period=2026-11&schedule={billing_seed['schedule']}",
        "run_detail": f"/money/assessments/runs/{billing_seed['run']}/",
        "schedule_new": "/money/assessments/schedules/new/",
        "schedule_edit": f"/money/assessments/schedules/{billing_seed['schedule']}/",
    }[kind]
    login(BOARD_EMAIL)
    resp = page.goto(path)
    assert resp and resp.status == 200, f"{path} returned {resp and resp.status}"
    if kind == "run_detail":
        expect(page.get_by_text("$3,432.36").first).to_be_visible()
    axe(page)
