"""E-notice consent (RCW 64.34.396).

Demo seed: owners A and G start with consent, C does not. Fakes only (Stripe fake, file mailbox).
"""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2
from helpers import seed as seed_helpers
from test_imports import import_base

SCAN = Path(__file__).parent / "fixtures" / "signed_consent.pdf"


@pytest.fixture
def residents_on(seed):
    seed_helpers.billing()
    m2.residents_enabled(True)


def _person_page(page, unit):
    page.goto(f"/units/{unit}/")
    page.locator("main a[href*='/people/']").first.click()
    expect(page).to_have_url(re.compile(r"/units/people/\d+/$"))


def test_owner_gives_consent_on_home_then_withdraws(page, residents_on, login, axe):
    login(m2.OWNERC_EMAIL)
    page.goto("/me/")
    prompt = page.get_by_test_id("consent-prompt")
    expect(prompt).to_be_visible()
    expect(prompt.get_by_test_id("consent-text")).to_contain_text("first-class mail")
    axe(page)
    prompt.get_by_label(re.compile("I agree to receive official association notices")).check()
    prompt.get_by_test_id("consent-give").click()
    expect(page.get_by_test_id("consent-prompt")).to_have_count(0)

    page.goto("/me/profile/")
    expect(page.get_by_text("You get official notices by email.")).to_be_visible()
    expect(page.get_by_test_id("consent-email")).to_have_text(m2.OWNERC_EMAIL)
    axe(page)
    page.get_by_test_id("consent-withdraw").click()
    expect(page.get_by_text("You get official notices by postal mail.")).to_be_visible()
    expect(page.get_by_test_id("consent-withdraw")).to_have_count(0)
    axe(page)


def test_board_records_paper_consent_with_scan(page, residents_on, login, axe):
    login(BOARD_EMAIL)
    _person_page(page, "C")
    form = page.get_by_test_id("paper-consent")
    # Email without a scan is refused.
    form.get_by_label("Notices by").select_option("email")
    form.get_by_label("Source").select_option("paper")
    form.get_by_role("button", name="Record preference").click()
    expect(page.get_by_text("Upload the scan of the signed consent form.")).to_be_visible()
    axe(page)

    form = page.get_by_test_id("paper-consent")
    form.get_by_label("Notices by").select_option("email")
    form.get_by_label("Source").select_option("paper")
    form.get_by_label("Scan of the signed consent form").set_input_files(SCAN)
    form.get_by_label("Note").fill("E2E signed form")
    form.get_by_role("button", name="Record preference").click()
    row = page.locator("table tbody tr").filter(has_text="E2E signed form")
    expect(row).to_contain_text("Signed paper form")
    expect(row).to_contain_text("Yes")
    link = row.get_by_role("link", name="signed form")
    resp = page.request.get(link.get_attribute("href"))
    assert resp.status == 200
    axe(page)


def test_import_never_creates_consent(page, seed_bare, login):
    login(BOARD_EMAIL)
    import_base(page)  # owner_directory.csv has E-Delivery = Yes for unit A
    _person_page(page, "A")
    row = page.locator("table tbody tr").filter(has_text="Imported e-delivery: yes")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Postal mail")
    expect(row).not_to_contain_text("Yes ·")


def _failed_autopay_email_bounced(base_url, mail):
    intent = m2.stripe_fake("last-intent")
    hook = m2.stripe_fake("webhook", "payment_intent.payment_failed", intent["id"])
    assert m2.post_webhook(base_url, hook["association_id"], hook["body"], hook["signature"]) == 200
    m2.drain_jobs()  # no worker on the E2E server: send the queued email now
    mail.wait_for(m2.OWNERA_EMAIL)
    m2.email_bounce(m2.OWNERA_EMAIL)
    mail.clear()


def test_two_bounces_revoke_consent(page, residents_on, login, mail, base_url, axe):
    """Owner A (seeded consent): the pre-debit notice and the failed-debit email both bounce."""
    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/")
    page.get_by_role("switch", name="Autopay").click()
    page.get_by_role("button", name="Turn on autopay").click()
    m2.run_scheduled("2026-10-26")  # Notice ~15 days before the Nov 10 debit
    m2.drain_jobs()
    mail.wait_for(m2.OWNERA_EMAIL)
    m2.email_bounce(m2.OWNERA_EMAIL)
    mail.clear()
    page.goto("/me/profile/")
    expect(page.get_by_text("You get official notices by email.")).to_be_visible()

    m2.run_scheduled("2026-11-10")
    _failed_autopay_email_bounced(base_url, mail)
    page.goto("/me/profile/")
    expect(page.get_by_text("You get official notices by postal mail.")).to_be_visible()
    expect(page.get_by_text("Two consecutive failed email deliveries")).to_be_visible()

    page.context.clear_cookies()
    login(BOARD_EMAIL)
    page.goto("/")
    todo = page.get_by_text(re.compile(r"Email consent ended for .+: notices now go by mail"))
    expect(todo.first).to_be_visible()
    axe(page)
