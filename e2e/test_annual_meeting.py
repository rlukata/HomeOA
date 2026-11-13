"""The annual meeting workflow on the seeded Dec 3 pilot meeting with the 2027 draft
budget. The Treasurer adopts the budget; the Secretary records the summary as sent, adds
the ratification and audit-waiver votes, owners A and G vote online, the Secretary enters
paper ballots for the rest, closes both votes and records the results: the waiver passes
and closes the 2026 annual_audit compliance period; the budget is rejected, which shows the
annual-meeting Home to-do until dismissed. Also: a compliance item marked done with an
attachment and the "approve minutes" Home to-do for draft minutes."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL
from helpers import m2
from helpers import seed as seed_helpers

UNITS = list("ABCDEFGHI")
ONLINE = {"A": "ownera@cedarrow.example", "G": OWNER_EMAIL}
REJECTED_TODO = "Budget rejected: confirm the dues schedule"

_PERIOD = """
import json
from homeoa.compliance.models import ObligationPeriod
p = ObligationPeriod.all_associations.filter(obligation_id="annual_audit", year=2026).first()
print("E2EJSON" + json.dumps({"status": p and p.status}))
"""


def _open_vote(page, name):
    page.get_by_test_id(name).get_by_role("link").first.click()
    expect(page.get_by_test_id("vote-status")).to_have_text("Draft")
    page.get_by_role("button", name="Open vote and send ballots").click()
    expect(page.get_by_test_id("vote-status")).to_have_text("Open")
    return page.url


def _cast_online(browser, base_url, login, email, vote_url, choice):
    ctx = browser.new_context(base_url=base_url)
    p = ctx.new_page()
    login(email, p)
    pk = vote_url.rstrip("/").rsplit("/", 1)[1]
    p.goto(f"/me/votes/{pk}/")
    form = p.get_by_test_id("ballot-form")
    form.get_by_label(choice, exact=True).check()
    form.get_by_role("button").click()
    expect(p.get_by_role("heading", name="Ballot received")).to_be_visible()
    ctx.close()


def _paper(page, vote_url, unit, choice, scan):
    page.goto(vote_url)
    form = page.get_by_test_id("paper-form")
    form.locator("select[name=unit]").select_option(label=unit)
    person = form.locator("select[name=person] option").filter(has_text=re.compile(rf"^{unit} · "))
    form.locator("select[name=person]").select_option(value=person.first.get_attribute("value"))
    form.locator("select[name^=q]").select_option(label=choice)
    form.get_by_label("Scan of the signed ballot").set_input_files(str(scan))
    form.get_by_role("button", name="Record paper ballot").click()
    expect(page.get_by_text(f"Paper ballot for unit {unit} recorded.")).to_be_visible()


def test_annual_meeting_workflow(page, browser, base_url, seed, login, axe, tmp_path):
    m2.residents_enabled(True)
    login(BOARD_EMAIL)
    page.goto("/")
    adopt = "Adopt next year's budget for the annual meeting Dec 3"
    expect(page.get_by_text(adopt)).to_be_visible()

    page.goto("/governance/meetings/")
    page.get_by_role("link", name="Annual owners meeting").first.click()
    page.get_by_test_id("annual-link").click()
    expect(page).to_have_url(re.compile(r"/governance/meetings/\d+/annual/$"))
    annual_url = page.url
    expect(page.get_by_test_id("step-adopt")).to_contain_text("still a draft")
    axe(page)

    # Adopt the 2027 budget.
    page.get_by_test_id("annual-budget").get_by_role("link").click()
    page.get_by_test_id("budget-adopt").click()
    expect(page.get_by_text("Budget adopted.")).to_be_visible()

    # The Secretary runs the meeting from here.
    login(SECRETARY_EMAIL)
    page.goto(annual_url)
    expect(page.get_by_test_id("step-adopt")).to_contain_text("Done")

    # Summary sent.
    page.get_by_role("button", name="Record as sent").click()
    expect(page.get_by_text("Budget summary recorded as sent.")).to_be_visible()
    expect(page.get_by_test_id("step-send")).to_contain_text("Done")

    # Votes on the agenda.
    page.get_by_role("button", name="Add votes to the agenda").click()
    expect(page.get_by_text(re.compile("Votes added to the agenda"))).to_be_visible()
    rat_url = _open_vote(page, "ratification-vote")
    page.goto(annual_url)
    waiver_url = _open_vote(page, "waiver-vote")

    # Ballots: A and G online, the rest on paper. All reject the budget, all waive.
    for email in ONLINE.values():
        _cast_online(browser, base_url, login, email, rat_url, "Reject the budget")
        _cast_online(browser, base_url, login, email, waiver_url, "Waive the audit")
    scan = tmp_path / "ballot.pdf"
    scan.write_bytes(b"%PDF-1.4\n% e2e scan\n")
    for unit in UNITS:
        if unit in ONLINE:
            continue
        _paper(page, rat_url, unit, "Reject the budget", scan)
        _paper(page, waiver_url, unit, "Waive the audit", scan)
    for url in (rat_url, waiver_url):
        page.goto(url)
        expect(page.get_by_test_id("turnout")).to_have_text("9 of 9 units voted")
        page.get_by_role("button", name="Close vote and record result").click()
        expect(page.get_by_test_id("vote-status")).to_have_text("Closed")

    # Record results.
    page.goto(annual_url)
    page.get_by_role("button", name="Record vote results").click()
    expect(
        page.get_by_text("Owners rejected the budget; the prior budget continues.")
    ).to_be_visible()
    expect(
        page.get_by_text("The audit is waived and the 2026 audit obligation is closed.")
    ).to_be_visible()
    expect(page.get_by_test_id("ratification-outcome")).to_have_text("Rejected")
    expect(page.get_by_test_id("waiver-outcome")).to_be_visible()
    expect(page.get_by_test_id("rejected-note")).to_be_visible()
    axe(page)
    period = seed_helpers.run_script(_PERIOD, "audit period")
    assert period["status"] not in (None, "open"), period

    # The annual-meeting to-do, then Dismiss clears it.
    page.goto("/")
    expect(page.get_by_text(REJECTED_TODO)).to_be_visible()
    page.goto(annual_url)
    page.get_by_role("button", name="Dismiss follow-up").click()
    expect(page.get_by_text("Follow-up dismissed.")).to_be_visible()
    page.goto("/")
    expect(page.get_by_text(REJECTED_TODO)).to_have_count(0)


def test_compliance_done_with_attachment(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    page.goto("/governance/compliance/items/add/")
    page.get_by_label("Title").fill("E2E Fire inspection")
    page.get_by_label("First due date").fill("2026-11-15")
    page.get_by_role("button", name="Save", exact=True).click()
    page.get_by_test_id("compliance-items").get_by_role("link", name="E2E Fire inspection").click()
    item_url = page.url
    proof = tmp_path / "fire-cert.pdf"
    proof.write_bytes(b"%PDF-1.4\n% e2e proof\n")
    page.get_by_label("Attachment (optional)").set_input_files(str(proof))
    page.get_by_role("button", name="Mark this occurrence done").click()
    expect(page.get_by_text("Marked as done.")).to_be_visible()
    page.goto(item_url)
    note = page.locator("p", has_text="Proof of last completion:")
    expect(note).to_be_visible()
    resp = page.request.get(note.get_by_role("link").get_attribute("href"))
    assert resp.ok, resp.status


def test_draft_minutes_todo(page, seed, login):
    login(SECRETARY_EMAIL)
    page.goto("/governance/meetings/")
    page.get_by_label("Kind").select_option(label="Board meeting")
    page.get_by_label("Title (optional)").fill("E2E Past board meeting")
    page.get_by_label("Date").fill("2026-10-01")
    page.get_by_role("button", name="Schedule meeting").click()
    page.get_by_test_id("minutes-link").click()
    page.get_by_test_id("minutes-body").fill("Called to order.")
    page.get_by_role("button", name="Save draft").click()
    expect(page.get_by_text("Draft minutes saved.")).to_be_visible()
    page.goto("/")
    todo = page.get_by_text("Approve minutes of E2E Past board meeting at the next meeting")
    expect(todo).to_be_visible()
