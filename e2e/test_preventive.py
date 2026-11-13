"""Preventive maintenance and components. The Maintenance committee sees the demo tasks
and components; the daily job opens a draft work order for the gutter cleaning due
Oct 15; completing it moves the task to April; a vendor-less task is a Home to-do."""

import re

from playwright.sync_api import expect

from conftest import manage
from helpers import m2

COMMITTEE = "maintenance@cedarrow.example"


def test_committee_preventive_and_components(page, seed, login, axe):
    r = manage("run_scheduled", "--as-of", "2026-10-07", "--job", "maintenance.preventive")
    assert r.returncode == 0, r.stderr[-1500:]
    m2.drain_jobs()
    login(COMMITTEE)
    page.goto("/")
    expect(page.get_by_text("Preventive maintenance due: Walk the fence line")).to_be_visible()
    page.goto("/requests/preventive/")
    tasks = page.get_by_test_id("tasks")
    expect(tasks).to_contain_text("Clean gutters and downspouts")
    axe(page)
    tasks.get_by_role("link", name="Clean gutters and downspouts").click()
    page.get_by_role("link", name=re.compile(r"^#\d+$")).first.click()
    expect(page).to_have_url(re.compile(r"/requests/work-orders/\d+/$"))
    page.goto("/requests/components/")
    comps = page.get_by_test_id("components")
    expect(comps).to_contain_text("Roof")
    expect(comps).to_contain_text("2033")
    expect(page.get_by_test_id("components-total")).to_contain_text("$322,500.00")
    axe(page)
    comps.get_by_role("link", name="Roof").click()
    expect(page.get_by_test_id("component-form")).to_be_visible()
    axe(page)
