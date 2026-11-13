"""Reusable UI steps shared by scenarios."""

from __future__ import annotations

import re


def post_dues(page, month: str = "2026-10") -> None:
    """Post a month's assessments (seed leaves October unposted)."""
    page.goto("/money/assessments/")
    page.get_by_role("button", name=re.compile("run .*assessment|post dues", re.IGNORECASE)).click()
    page.locator("#id_period").fill(month)
    page.get_by_role("button", name="Post charges").click()


def record_payment(page, unit: str, amount: str, ref: str):
    page.goto("/money/payments/new/")
    page.locator("#id_unit").select_option(label=f"Unit {unit}")
    page.locator("#id_amount").fill(amount)
    page.locator("#id_received_date").fill("2026-10-05")
    page.locator("#id_method").select_option(label="Check")
    page.locator("#id_reference").fill(ref)
    page.get_by_role("button", name="Record payment").click()
    return page.get_by_test_id("allocation")


def approve_change(page, change_url: str, email: str = "") -> None:
    """Another board member (the secretary by default) approves a pending change request
    in their own browser; the change is then made."""
    from urllib.parse import urlsplit

    from playwright.sync_api import expect

    from conftest import SECRETARY_EMAIL
    from helpers.auth import login_as

    base_url = "{0.scheme}://{0.netloc}".format(urlsplit(page.url))
    ctx = page.context.browser.new_context(base_url=base_url)
    p = ctx.new_page()
    login_as(p, base_url, email or SECRETARY_EMAIL)
    p.goto(change_url)
    p.get_by_test_id("change-approve").click()
    expect(p.get_by_text("Approved. The change has been made.")).to_be_visible()
    expect(p.get_by_test_id("change-status")).to_have_text("Approved and applied")
    ctx.close()


def propose_and_approve(page, email: str = "") -> None:
    """The form just submitted went for approval; another board member approves it and
    the page returns to the result."""
    change_url = expect_sent_for_approval(page)
    approve_change(page, change_url, email)
    page.goto(change_url)


def expect_sent_for_approval(page) -> str:
    """The form went to the board as a change request; returns its page."""
    from playwright.sync_api import expect

    expect(page.get_by_text(re.compile(r"Sent for board approval"))).to_be_visible()
    expect(page.get_by_test_id("change-status")).to_have_text("Waiting for approval")
    return page.url


def propose_role(page, email: str, role: str) -> None:
    """Settings > Roles: propose a role for someone; the secretary approves it from the
    pending list on the same page."""
    from playwright.sync_api import expect

    page.goto("/settings/roles/")
    form = page.locator("form[action$='/assign/']")
    form.get_by_label("Email").fill(email)
    form.get_by_label("Role").select_option(label=role)
    form.get_by_label("Start date").fill("2026-01-01")
    form.get_by_role("button", name="Propose role").click()
    expect(page.get_by_text(re.compile(r"Sent for board approval"))).to_be_visible()
    pending = page.get_by_test_id("pending-role-changes").get_by_role("link", name=email)
    approve_change(page, pending.get_attribute("href"))
