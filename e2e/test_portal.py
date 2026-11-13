"""Owner portal scenarios.

Hooks they use are in helpers/m2.py (see e2e/README.md).
"""

import re
from decimal import Decimal

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, TENANT_EMAIL, manage
from helpers import m2
from helpers import seed as seed_helpers

OWNER_TABS = ["Home", "Pay", "Requests", "Docs", "More"]
# Unit C October dues under the real A-I percentages.
DUES_C = "$396.63"
# OWNER_EMAIL (conftest) is unit G's owner.
TENANT_TABS = ["Home", "Requests", "Docs", "More"]


@pytest.fixture
def billed(seed):
    """seed_demo plus October 2026 dues posted."""
    return seed_helpers.billing()


@pytest.fixture
def residents_on(billed):
    m2.residents_enabled(True)
    return billed


def tab_names(page):
    links = page.get_by_role("navigation", name="Hubs").get_by_role("link").all_inner_texts()
    return [t.strip() for t in links]


# 0a
def test_residents_disabled_login(page, billed, login, axe):
    # Switch is off by default: the login page answers the same, with a note.
    page.goto("/login/")
    page.get_by_label(re.compile("email", re.I)).fill(m2.OWNERC_EMAIL)
    page.get_by_role("button", name="Email me a sign-in link").click()
    expect(page.get_by_test_id("residents-closed")).to_contain_text("not open to residents yet")
    axe(page)
    # Even a valid session gets the "not open yet" page (403) on every /me/ tab.
    login(m2.OWNERC_EMAIL)
    for path in ["/me/", "/me/pay/"]:
        resp = page.goto(path)
        assert resp and resp.status == 403, f"{path} returned {resp and resp.status}"
        expect(page.get_by_test_id("residents-closed")).to_be_visible()
    expect(page.get_by_role("navigation", name="Hubs")).to_have_count(0)
    axe(page)


# 0a': switch on shows the shell with the right tabs per role.
@pytest.mark.phone
def test_resident_tabs_by_role(phone_page, residents_on, login, axe):
    login(m2.OWNERC_EMAIL, phone_page)
    phone_page.goto("/me/")
    assert tab_names(phone_page) == OWNER_TABS
    axe(phone_page)
    phone_page.context.clear_cookies()
    login(TENANT_EMAIL, phone_page)
    phone_page.goto("/me/")
    assert tab_names(phone_page) == TENANT_TABS
    resp = phone_page.goto("/me/pay/")
    assert resp and resp.status == 403


# 1
@pytest.mark.phone
def test_owner_dashboard(phone_page, residents_on, login, axe):
    login(m2.OWNERC_EMAIL, phone_page)
    phone_page.goto("/me/")
    assert tab_names(phone_page) == OWNER_TABS
    expect(phone_page.get_by_test_id("balance")).to_have_text(DUES_C)
    expect(phone_page.get_by_test_id("due-line")).to_have_text(re.compile(r"Due \w{3}, Oct 10"))
    expect(phone_page.get_by_role("link", name=f"Pay {DUES_C}")).to_be_visible()
    axe(phone_page)


# 2
@pytest.mark.phone
def test_tenant_no_finances(phone_page, residents_on, login, axe):
    login(TENANT_EMAIL, phone_page)
    phone_page.goto("/me/")
    assert tab_names(phone_page) == TENANT_TABS
    note = re.compile(r"tenant of Unit E\b", re.I)
    expect(phone_page.get_by_text(note)).to_be_visible()
    expect(phone_page.get_by_test_id("balance")).to_have_count(0)
    for path in ["/me/pay/", "/me/payments/", "/me/statements/", "/money/"]:
        resp = phone_page.goto(path)
        assert resp and resp.status == 403, f"{path} returned {resp and resp.status}"
    phone_page.goto("/me/")
    expect(phone_page.get_by_test_id("announcements")).to_be_visible()
    expect(phone_page.get_by_test_id("rules")).to_be_visible()
    # TODO: real announcements and Docs (owner-only docs hidden from tenants).
    axe(phone_page)


def _pay_now(page, amount=DUES_C):
    page.goto("/me/pay/")
    page.get_by_role("button", name=f"Pay {amount}").click()
    page.get_by_role("button", name=f"Confirm payment of {amount}").click()


# 3
def test_ach_pay_now_fake(page, residents_on, login, base_url, axe):
    login(m2.OWNERC_EMAIL)
    _pay_now(page)
    expect(page.get_by_test_id("payment-status")).to_contain_text("processing", ignore_case=True)
    intent = m2.stripe_fake("last-intent")
    # "amount" is what was charged. payments.fee_pass_through_allowed is unset in the
    # WA pack, so pass-through is off by default: the charge equals the dues.
    assert intent["amount"] == DUES_C[1:], f"charged {intent['amount']}, dues {DUES_C}"
    assert intent["status"] in ("processing", "pending")
    axe(page)

    hook = m2.stripe_fake("webhook", "payment_intent.succeeded", intent["id"])
    assert m2.post_webhook(base_url, hook["association_id"], hook["body"], hook["signature"]) == 200
    page.goto("/me/")
    expect(page.get_by_test_id("balance")).to_have_text("$0.00")

    # Replay is a no-op (idempotent); bad signature is refused.
    assert m2.post_webhook(base_url, hook["association_id"], hook["body"], hook["signature"]) == 200
    assert m2.post_webhook(base_url, hook["association_id"], hook["body"], "t=1,v1=bad") == 400
    page.goto("/me/payments/")
    expect(page.get_by_text(f"{DUES_C} · ACH (online)")).to_have_count(1)

    page.context.clear_cookies()
    login(BOARD_EMAIL)
    page.goto("/units/C/")
    expect(page.get_by_test_id("unit-balance")).to_have_text("$0.00")
    page.goto("/money/reports/trial-balance/")
    expect(page.get_by_test_id("tb-difference")).to_have_text("$0.00")


# 4
def test_ach_failure(page, residents_on, login, base_url):
    login(m2.OWNERC_EMAIL)
    _pay_now(page)
    intent = m2.stripe_fake("last-intent")
    ok = m2.stripe_fake("webhook", "payment_intent.succeeded", intent["id"])
    m2.post_webhook(base_url, ok["association_id"], ok["body"], ok["signature"])
    failed = m2.stripe_fake("webhook", "payment_intent.payment_failed", intent["id"])
    status = m2.post_webhook(
        base_url, failed["association_id"], failed["body"], failed["signature"]
    )
    assert status == 200
    page.goto("/me/")
    expect(page.get_by_test_id("balance")).to_have_text(DUES_C)

    page.context.clear_cookies()
    login(BOARD_EMAIL)
    page.goto("/")
    expect(
        page.get_by_text(re.compile(r"ACH payment .*unit C.* failed", re.I)).first
    ).to_be_visible()
    page.goto("/units/C/")
    expect(page.get_by_role("cell", name=re.compile(r"Void of payment \d+"))).to_be_visible()


# No prepayment. An amount over the balance is refused before Stripe sees it.
def test_pay_more_than_balance_refused(page, residents_on, login):
    login(m2.OWNERC_EMAIL)
    page.goto("/me/pay/")
    expect(page.get_by_text(f"Up to your balance of {DUES_C}.")).to_be_visible()
    over = f"{float(DUES_C[1:]) + 1:.2f}"
    page.get_by_label("Another amount").fill(over)
    page.get_by_role("button", name="Pay this").click()
    expect(page.get_by_text(f"You can pay at most your balance of {DUES_C}.")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Confirm payment"))).to_have_count(0)
    r = manage("stripe_fake", "last-intent", check=False)
    assert r.returncode and "No intents yet" in r.stderr, r.stdout


# 5: full-balance autopay gets a pre-debit notice ~15 days ahead and debits on the
# 10th, never more than the noticed amount.
def test_autopay_enroll_and_run(page, residents_on, login, mail, axe):
    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/")
    page.get_by_role("switch", name="Autopay").click()
    page.get_by_role("button", name="Turn on autopay").click()
    expect(page.get_by_test_id("autopay-line")).to_contain_text("Autopay is on from")
    axe(page)

    m2.run_scheduled("2026-10-26")
    m2.drain_jobs()
    msg = mail.wait_for(m2.OWNERA_EMAIL)
    m = re.search(r"Autopay of up to \$([\d,]+\.\d\d) for unit A on November 10, 2026", msg.subject)
    assert m, msg.subject
    noticed = Decimal(m.group(1).replace(",", ""))
    assert f"${m.group(1)}" in msg.body and "November 10, 2026" in msg.body, msg.body[:400]
    r = manage("stripe_fake", "last-intent", check=False)
    assert r.returncode and "No intents yet" in r.stderr  # notice only, no debit yet

    out = m2.run_scheduled("2026-11-10")
    assert "assessment" in out.lower()  # November dues post before the debit
    charged = Decimal(m2.stripe_fake("last-intent")["amount"])
    assert Decimal("0") < charged <= noticed, f"charged {charged}, noticed {noticed}"

    page.context.clear_cookies()
    login(BOARD_EMAIL)
    page.goto("/")
    expect(page.get_by_text(re.compile(r"Autopay collected from \d+ units?"))).to_be_visible()


# Board autopay settings; the notice lead cannot go below the rule-pack minimum.
def test_autopay_settings_lead_minimum(page, residents_on, login, axe):
    login(BOARD_EMAIL)
    page.goto("/money/payments/")
    page.get_by_test_id("autopay-settings-link").click()
    expect(page).to_have_url(re.compile(r"/money/payments/autopay/settings/$"))
    expect(page.get_by_test_id("autopay-next-cycle")).to_contain_text(
        "Notice on Oct 26, 2026, debit on Nov 10, 2026."
    )
    axe(page)
    page.get_by_role("spinbutton", name=re.compile("^Debit day")).fill("10")
    page.get_by_role("spinbutton", name=re.compile(r"^Notice lead")).fill("9")
    page.get_by_role("button", name="Save").click()
    err = "The notice must go out at least 10 days before the debit."
    expect(page.get_by_text(err)).to_be_visible()
    axe(page)
    page.get_by_role("spinbutton", name=re.compile(r"^Notice lead")).fill("10")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("The notice must go out at least")).to_have_count(0)


def _consent_c(page, login):
    """Unit C's owner gives e-notice consent on Home; C starts without it."""
    login(m2.OWNERC_EMAIL)
    page.goto("/me/")
    prompt = page.get_by_test_id("consent-prompt")
    prompt.get_by_label(re.compile("I agree to receive official association notices")).check()
    prompt.get_by_test_id("consent-give").click()
    expect(page.get_by_test_id("consent-prompt")).to_have_count(0)
    page.context.clear_cookies()


def _generate_october(page):
    page.goto("/money/statements/")
    page.get_by_role("button", name="Generate statements for October 2026").click()
    expect(page).to_have_url(re.compile(r"/money/statements/\d+/$"))


# 6
def test_statement_delivery(page, residents_on, login, mail, axe):
    _consent_c(page, login)
    login(BOARD_EMAIL)
    _generate_october(page)
    page.get_by_test_id("send-statements").click()
    expect(page.get_by_text(re.compile(r"Sent \d+ statements"))).to_be_visible()
    axe(page)

    m2.drain_jobs()  # no worker on the E2E server
    msg = mail.wait_for(m2.OWNERC_EMAIL)
    assert "statement" in msg.subject.lower(), msg.subject
    assert "Oct 1, 2026" in msg.body, msg.body[:300]
    table = page.get_by_test_id("statements")
    for unit, channel in [("C", "Email"), ("G", "Email"), ("B", "Mail")]:
        row = table.locator(f'tr[data-unit="{unit}"]')
        expect(row.get_by_test_id("channel")).to_have_text(channel)
        expect(row.get_by_test_id("status")).to_have_text("Sent")
    expect(page.get_by_test_id("mail-batch").first).to_be_visible()
    # Everything sent: the button is disabled, re-sending is a no-op.
    expect(page.get_by_test_id("send-statements")).to_be_disabled()

    for unit in ["C", "B"]:
        page.goto(f"/units/{unit}/comms/")
        log = page.get_by_test_id("comm-log")
        expect(log).to_contain_text("statement", ignore_case=True)
        expect(log.locator("tbody tr").first).to_have_attribute(
            "data-status", re.compile("sent|queued|delivered")
        )


# 7
def test_bounce_marks_failed(page, residents_on, login, mail):
    # Unit G's owner has e-notice consent in the demo seed.
    login(BOARD_EMAIL)
    _generate_october(page)
    page.goto("/units/G/")
    page.get_by_test_id("unit-statements").get_by_test_id("send-statement").click()
    expect(page.get_by_text(re.compile(r"Statement for unit G sent \(Email\)"))).to_be_visible()
    m2.drain_jobs()  # no worker on the E2E server
    mail.wait_for(OWNER_EMAIL)
    m2.email_bounce(OWNER_EMAIL)
    page.goto("/units/G/comms/")
    expect(page.get_by_test_id("comm-log").locator('tr[data-status="bounced"]')).to_have_count(1)
    page.goto("/")
    expect(page.get_by_text(re.compile("Email to unit G bounced", re.I)).first).to_be_visible()


# 8
def test_delinquency_ladder(page, billed, login, mail, axe):
    """Fixed clock 2026-10-07, residents off: the ladder previews, nothing is sent."""
    login(BOARD_EMAIL)
    page.goto("/money/")
    page.get_by_role("link", name="Delinquency", exact=True).first.click()
    expect(page).to_have_url(re.compile(r"/money/delinquency/$"))
    expect(page.get_by_test_id("delinquency-offsets").locator("li")).to_have_count(3)
    axe(page)

    page.get_by_role("button", name="Check the ladder now (Oct 7, 2026)").click()
    expect(page.get_by_text(re.compile(r"Ladder checked: [1-9]\d* step"))).to_be_visible()
    row = page.get_by_test_id("delinquency-cases").locator('tr[data-unit="G"]')
    expect(row).to_have_count(1)
    expect(row.locator('[data-step="reminder"]')).to_contain_text("Payment reminder")
    expect(row.get_by_role("link", name="Letter").first).to_be_visible()
    axe(page)
    assert mail.messages() == []  # residents off: previews and drafts only

    page.goto("/settings/delinquency/")
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_visible()
    axe(page)


# 9
def test_preferences_consent(page, residents_on, login, mail):
    _consent_c(page, login)
    login(m2.OWNERC_EMAIL)
    page.goto("/me/profile/")
    expect(page.get_by_text("You get official notices by email.")).to_be_visible()
    page.context.clear_cookies()

    login(BOARD_EMAIL)
    _generate_october(page)
    page.goto("/units/C/")
    page.get_by_test_id("unit-statements").get_by_test_id("send-statement").click()
    expect(page.get_by_text(re.compile(r"Statement for unit C sent \(Email\)"))).to_be_visible()
    m2.drain_jobs()  # no worker on the E2E server
    mail.wait_for(m2.OWNERC_EMAIL)
    page.goto("/units/C/comms/")
    expect(page.get_by_test_id("comm-log")).to_contain_text("Email")
