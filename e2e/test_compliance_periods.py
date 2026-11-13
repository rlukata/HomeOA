"""Compliance calendar: the daily job opens periods; paths, evidence, exemptions,
missed-deadline to-do, and owners refused."""

import re

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import m2

CAL = "/governance/compliance/"


@pytest.fixture
def opened(seed):
    m2.run_scheduled("2026-10-08")  # annual obligations open 120 days before Dec 31
    return seed


def _open_period(page, ob_id):
    page.goto(CAL)
    page.locator(f'[data-testid="compliance-open"] tr[data-obligation="{ob_id}"] a').click()
    expect(page).to_have_url(re.compile(r"/governance/compliance/\d+/$"))


def test_job_opens_periods(page, opened, login, axe):
    login(BOARD_EMAIL)
    page.goto(CAL)
    table = page.get_by_test_id("compliance-open")
    for ob in ["annual_audit", "reserve_study_update", "budget_ratification"]:
        expect(table.locator(f'tr[data-obligation="{ob}"]')).to_contain_text("Choose a path")
    axe(page)
    page.goto("/")
    expect(
        page.get_by_text("Choose how to meet: Annual audit or waiver vote (2026)")
    ).to_be_visible()


def test_path_evidence_done(page, opened, login, axe):
    login(BOARD_EMAIL)
    _open_period(page, "annual_audit")
    axe(page)
    page.get_by_role("button", name="Mark as done").click()
    expect(page.get_by_text("Choose which path the association took first.")).to_be_visible()
    page.get_by_role("radio", name=re.compile("Owner vote waiving the audit")).check()
    page.get_by_role("button", name="Save path").click()
    expect(page.get_by_role("radio", name=re.compile("Owner vote waiving"))).to_be_checked()
    page.get_by_role("button", name="Mark as done").click()
    expect(page.get_by_text("Add the evidence first (a document or a link).")).to_be_visible()
    page.get_by_label("Or a link (e.g. to the owner vote)").fill("https://example.org/vote")
    page.get_by_label("Note").fill("Waiver vote passed")
    page.get_by_role("button", name="Add evidence").click()
    expect(page.get_by_test_id("evidence")).to_contain_text("Waiver vote passed")
    page.get_by_role("button", name="Mark as done").click()
    expect(page.get_by_test_id("period-status")).to_have_text("Done")
    page.goto(CAL)
    expect(page.get_by_test_id("compliance-closed")).to_contain_text("Annual audit")
    expect(
        page.get_by_test_id("compliance-open").locator('tr[data-obligation="annual_audit"]')
    ).to_have_count(0)


def test_exemption_yes_closes(page, opened, login):
    login(BOARD_EMAIL)
    _open_period(page, "reserve_study_update")
    page.get_by_test_id("exemptions").get_by_role("button", name="Yes").first.click()
    expect(page.get_by_test_id("period-status")).to_contain_text("Exempt")
    page.goto(CAL)
    expect(page.get_by_test_id("compliance-closed")).to_contain_text("Annual reserve study update")


def test_missed_deadline_todo(page, opened, login):
    m2.run_scheduled("2027-01-02")
    login(BOARD_EMAIL)
    page.goto("/")
    expect(
        page.get_by_text(
            "Missed deadline: Budget: send to owners and hold the ratification meeting (2026)"
        )
    ).to_be_visible()
    page.goto(CAL)
    row = page.get_by_test_id("compliance-open").locator(
        'tr[data-obligation="budget_ratification"]', has_text="2026"
    )
    expect(row).to_contain_text("Missed deadline")
    row.locator("a").click()
    expect(page.get_by_test_id("missed")).to_be_visible()


def test_owner_refused(page, opened, login):
    login(OWNER_EMAIL)
    resp = page.goto(CAL)
    assert resp and resp.status == 403, resp and resp.status
