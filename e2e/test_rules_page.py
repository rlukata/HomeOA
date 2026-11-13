"""/settings/rules/ shows citations, badges, dated values and statutory caps.

The demo/E2E seed runs the WA pack (statute-checked, effective-dated values,
caps on the pre-notice late fee and the interest rate). Overriding a rule is a policy
change: the treasurer proposes it and the secretary approves.
"""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.flows import propose_and_approve


def _row(page, key):
    return page.locator(f'tr[data-rule-key="{key}"]')


def _approved(page):
    propose_and_approve(page)
    page.goto("/settings/rules/")


def test_rules_page_citations_badges_override(page, seed, login, axe):
    login(BOARD_EMAIL)
    resp = page.goto("/settings/rules/")
    assert resp and resp.status == 200, f"rules returned {resp and resp.status}"
    expect(page.get_by_text(re.compile(r"Active rule pack: .*Washington"))).to_be_visible()
    axe(page)

    fee = _row(page, "late_fees.amount")
    expect(fee).to_contain_text("$25.00")
    # The WA pack leaves this key to the generic pack, so it is labeled as such.
    expect(fee.locator('[data-source="generic"]')).to_have_text("Generic default")
    expect(fee).to_contain_text("HomeOA default")
    expect(fee.locator(".badge.warn")).to_have_text("Unverified")
    cap = _row(page, "late_fees.cap")
    expect(cap).to_contain_text("Generic default")
    expect(cap).to_contain_text("RCW 64.34.364(13)")
    # Statute rows from the WA pack are verified.
    pre = _row(page, "late_fees.pre_notice.max_amount")
    expect(pre).to_contain_text("$50.00")
    expect(pre).to_contain_text("RCW 64.34.364(17)(b)")
    expect(pre.locator(".badge.ok")).to_have_text("Verified")
    expect(_row(page, "interest.annual_rate_max")).to_contain_text("12%")

    # Override a capped key below the cap: not clamped.
    key = "late_fees.pre_notice.max_amount"
    page.locator("#id_key").select_option(key)
    page.get_by_label("New value").fill("25.00")
    page.get_by_label("Reason").fill("E2E board decision")
    page.get_by_role("button", name="Save override").click()
    _approved(page)
    row = _row(page, key)
    expect(row).to_contain_text("Association setting")
    expect(row).to_contain_text("25.00")
    expect(row.locator(".badge.danger")).to_have_count(0)
    axe(page)

    page.locator("#id_key").select_option(key)
    page.get_by_label("Reason").fill("E2E clear")
    page.get_by_role("button", name="Clear override").click()
    _approved(page)
    expect(_row(page, key)).not_to_contain_text("Association setting")


def test_rules_page_link_quote_next_value_clamp(page, seed, login, axe):
    """WA pack: source links, statute quotes, dated values; an override above a cap clamps."""
    login(BOARD_EMAIL)
    page.goto("/settings/rules/")
    expect(page.locator("tbody a[href^='http'][target=_blank]").first).to_be_visible()
    pre = _row(page, "late_fees.pre_notice.max_amount")
    pre.locator("summary", has_text="Statute text").click()
    expect(pre.locator("blockquote")).to_be_visible()
    quorum = _row(page, "meetings.owner.quorum_percent")
    expect(quorum.locator("[data-next-value]")).to_contain_text("From Jan 1, 2028: 20%")

    # Board sets a $75 pre-notice late fee: above the $50 statutory cap, so it clamps.
    key = "late_fees.pre_notice.max_amount"
    page.locator("#id_key").select_option(key)
    page.get_by_label("New value").fill("75.00")
    page.get_by_label("Reason").fill("E2E over the cap")
    page.get_by_role("button", name="Save override").click()
    _approved(page)
    row = _row(page, key)
    expect(row).to_contain_text("Association setting")
    expect(row).to_contain_text("the legal maximum applies")
    expect(row).to_contain_text("$50.00")
    expect(row.locator(".badge.danger", has_text="Capped by statute")).to_be_visible()
    axe(page)

    page.locator("#id_key").select_option(key)
    page.get_by_label("Reason").fill("E2E clear")
    page.get_by_role("button", name="Clear override").click()
    _approved(page)
    expect(_row(page, key)).not_to_contain_text("Association setting")


def test_rules_page_governance_keys_and_generic_default(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/settings/rules/")
    # Keys the WA pack leaves unset fall back to the generic pack and say so.
    generic = page.locator('tbody [data-source="generic"]')
    expect(generic.first).to_have_text("Generic default")
    for key in (
        "notices.board_meeting.min_days",
        "notices.board_meeting.emergency_min_days",
        "votes.audit_waiver.threshold_percent",
        "votes.ballot_retention_months",
        "votes.electronic.express_authority",
        "votes.paper_ballot_for_nonconsenting",
    ):
        row = _row(page, key)
        expect(row).to_have_count(1)
        expect(row).to_contain_text("RCW 64.")
        expect(row.locator('[data-source="generic"]')).to_have_count(0)
    for key in (
        "notices.board_meeting.min_days",
        "notices.board_meeting.emergency_min_days",
        "votes.ballot_retention_months",
        "votes.electronic.express_authority",
        "votes.paper_ballot_for_nonconsenting",
    ):
        # Quoted from the statute text (research C, 2026-10-11).
        expect(_row(page, key).locator(".badge.warn")).to_have_count(0)
        expect(_row(page, key)).to_contain_text("Verified")
    axe(page)


def test_rules_page_threshold_comparison_keys(page, seed, login):
    login(BOARD_EMAIL)
    page.goto("/settings/rules/")
    for key in ("votes.audit_waiver.threshold_comparison", "budget.rejection_comparison"):
        row = _row(page, key)
        expect(row).to_have_count(1)
        expect(row).to_contain_text("RCW 64.")
        expect(row.locator(".badge.warn")).to_have_count(0)  # quoted from the statute
        expect(row).to_contain_text("Verified")
