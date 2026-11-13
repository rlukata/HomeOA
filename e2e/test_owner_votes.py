"""Owner vote (audit waiver) end to end on the pilot's 9 units (A-I).

Board creates and opens a rule-pack audit-waiver vote -> consenting owners (A Maya, G Abdi)
get an email through notify; the 7 units without consent get letters in a draft batch
"Ballots: ...". A and G vote online and get receipts; A changes the ballot. The board enters
C's paper ballot with a scan. Home reads "6 units outstanding". Close -> result, threshold
with its unverified badge, hash check, audit trail. Tenants get 403 on /me/votes/.
"""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, TENANT_EMAIL
from helpers import m2

TITLE = "Waive the 2026 audit"
QUESTION = "Waive the independent audit of the 2026 financial statements?"
PAPER_UNITS = ["B", "C", "D", "E", "F", "H", "I"]


def _cast(page, vote_url: str, choice: str) -> str:
    page.goto(vote_url)
    form = page.get_by_test_id("ballot-form")
    form.get_by_label(choice, exact=True).check()
    form.get_by_role("button").click()
    expect(page.get_by_role("heading", name="Ballot received")).to_be_visible()
    rid = page.get_by_test_id("receipt-id").inner_text().strip()
    assert re.fullmatch(r"[0-9A-F]{10}", rid), rid
    return rid


def test_owner_vote_audit_waiver(page, browser, base_url, seed, login, axe, mail, tmp_path):
    m2.residents_enabled(True)
    login(BOARD_EMAIL)

    # Create (draft) and open.
    page.goto("/governance/votes/")
    form = page.get_by_test_id("new-vote")
    form.get_by_label("Title").fill(TITLE)
    form.get_by_label("Opens").fill("2026-10-01T09:00")
    form.get_by_label("Closes").fill("2026-12-31T17:00")
    form.get_by_label("Questions").fill(QUESTION)
    form.get_by_label("Passes with").select_option("rule_pack")
    form.get_by_label("Rule-pack vote").select_option("audit_waiver")
    form.get_by_role("button", name="Save draft").click()
    expect(page).to_have_url(re.compile(r"/governance/votes/\d+/$"))
    vote_url = page.url
    vote_pk = int(vote_url.rstrip("/").rsplit("/", 1)[1])
    expect(page.get_by_test_id("vote-status")).to_have_text("Draft")
    page.get_by_role("button", name="Open vote and send ballots").click()
    expect(page.get_by_test_id("vote-status")).to_have_text("Open")
    axe(page)

    page.goto("/governance/votes/")
    expect(page.get_by_test_id("votes")).to_contain_text(TITLE)
    axe(page)

    # Emails to the consenting owners only.
    m2.drain_jobs()
    portal_link = f"/me/votes/{vote_pk}/"
    for to in ("ownera@cedarrow.example", OWNER_EMAIL):
        msg = mail.wait_for(to)
        assert msg.subject == f"Ballot: {TITLE}", msg.subject
        assert portal_link in msg.body, msg.body[:500]
    sent_to = {a.lower() for m in mail.messages() if TITLE in m.subject for a in m.to}
    assert sent_to == {"ownera@cedarrow.example", OWNER_EMAIL}, sent_to

    # Paper ballot letters for the 7 units without consent, in one draft batch.
    page.goto("/settings/letters/batches/")
    page.get_by_role("link", name=f"Ballots: {TITLE}").click()
    expect(page.get_by_role("heading", name=f"Ballots: {TITLE}")).to_be_visible()
    items = page.get_by_role("table", name="Letters in this batch")
    expect(items.locator("tbody tr")).to_have_count(len(PAPER_UNITS))

    # Online votes: G (owner7) and A (ownera); A then changes the ballot.
    ctx = browser.new_context(base_url=base_url)
    op = ctx.new_page()
    login(OWNER_EMAIL, op)
    op.goto("/me/votes/")
    expect(op.get_by_test_id("open-votes")).to_contain_text(TITLE)
    axe(op)
    op.goto(portal_link)
    axe(op)
    _cast(op, portal_link, "Yes")
    ctx.close()

    ctx = browser.new_context(base_url=base_url)
    ap = ctx.new_page()
    login("ownera@cedarrow.example", ap)
    first = _cast(ap, portal_link, "No")
    ap.goto(portal_link)
    expect(ap.get_by_test_id("already-voted")).to_contain_text("Unit A voted")
    expect(ap.get_by_role("button", name="Change my ballot")).to_be_visible()
    second = _cast(ap, portal_link, "Yes")
    assert first != second
    ctx.close()

    # Paper ballot for C (Priya), with a scan.
    scan = tmp_path / "ballot-c.pdf"
    scan.write_bytes(b"%PDF-1.4\n% e2e scan\n")
    page.goto(vote_url)
    paper = page.get_by_test_id("paper-form")
    paper.locator("select[name=unit]").select_option(label="C")
    paper.locator("select[name=person]").select_option(label="C · Priya Raman")
    paper.locator("select[name^=q]").select_option(label="Yes")
    paper.get_by_label("Scan of the signed ballot").set_input_files(str(scan))
    paper.get_by_role("button", name="Record paper ballot").click()
    expect(page.get_by_text("Paper ballot for unit C recorded.")).to_be_visible()
    expect(page.get_by_test_id("turnout")).to_have_text("3 of 9 units voted")
    ballots = page.get_by_test_id("ballots")
    expect(ballots).to_contain_text("ballot-c.pdf")
    expect(ballots.locator("tbody tr")).to_have_count(4)  # A twice (one replaced), C, G
    expect(ballots).to_contain_text("replaced")

    # Home to-do.
    page.goto("/")
    expect(page.get_by_text(re.compile(r"Vote closes Dec 31: 6 units outstanding"))).to_be_visible()

    # Close: result, threshold + unverified badge, hash check, audit trail.
    page.goto(vote_url)
    page.get_by_role("button", name="Close vote and record result").click()
    expect(page.get_by_test_id("vote-status")).to_have_text("Closed")
    expect(page.get_by_role("heading", name="Result")).to_be_visible()
    res = page.get_by_test_id("question-result")
    expect(res).to_contain_text(QUESTION)
    # 3 of 9 units cannot reach a rule-pack share of all votes.
    expect(res.locator(".badge")).to_have_text("Does not pass")
    card = page.locator("section[aria-labelledby='res-h']")
    expect(card).to_contain_text("Threshold: at least 60%")
    # The WA pack's audit-waiver 60% (RCW 64.34.372(1)) is quoted, so verified: no badge.
    expect(card.locator(".badge", has_text="unverified")).to_have_count(0)
    expect(page.get_by_test_id("ballots-hash")).to_contain_text("ballots unchanged since close")
    trail = page.get_by_test_id("audit-trail")
    expect(trail.locator("li")).not_to_have_count(0)
    expect(trail).not_to_contain_text("Nothing recorded yet.")
    for action in (
        "Ballot letters sent",
        "Ballot cast · Waive the 2026 audit · Unit G",
        "Ballot replaced · Waive the 2026 audit · Unit A",
        "Ballot cast · Waive the 2026 audit · Unit C · " + BOARD_EMAIL,
        "Vote closed",
    ):
        expect(trail).to_contain_text(action)
    axe(page)

    # Tenant: 403.
    ctx = browser.new_context(base_url=base_url)
    tp = ctx.new_page()
    login(TENANT_EMAIL, tp)
    resp = tp.goto("/me/votes/")
    assert resp and resp.status == 403, f"/me/votes/ returned {resp and resp.status}"
    ctx.close()
