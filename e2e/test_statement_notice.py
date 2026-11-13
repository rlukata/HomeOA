"""Statements print the next autopay debit and serve as the pre-debit notice.

October's statement is generated after Oct 26 (Nov's notice date) in effect (as of Oct 31),
so it prints the December 10 debit. A balance-rule statement sent now is December's
notice; a fixed-amount line is informational and fixed autopay gets no notice at all.
"""

import io
import re

import pytest
from playwright.sync_api import expect
from pypdf import PdfReader

from conftest import BOARD_EMAIL
from helpers import m2
from helpers import seed as seed_helpers

AUTOPAY_RE = r"Autopay up to \$[\d,]+\.\d\d on Dec 10"


@pytest.fixture
def residents_on(seed):
    billed = seed_helpers.billing()
    m2.residents_enabled(True)
    return billed


def _enroll_a(page, login, fixed: str | None = None):
    login(m2.OWNERA_EMAIL)
    page.goto("/me/pay/autopay/?unit=A")
    if fixed:
        page.get_by_role("radio", name=re.compile("Fixed amount")).check()
        page.get_by_label("Fixed amount", exact=True).fill(fixed)
    else:
        page.get_by_role("radio", name=re.compile("Statement balance")).check()
    page.get_by_role("button", name="Turn on autopay").click()
    page.goto("/me/pay/")
    expect(page.get_by_test_id("autopay-line")).to_contain_text("Autopay is on from")
    page.context.clear_cookies()


def _generate_and_send(page, login):
    login(BOARD_EMAIL)
    page.goto("/money/statements/")
    page.get_by_role("button", name="Generate statements for October 2026").click()
    expect(page).to_have_url(re.compile(r"/money/statements/\d+/$"))
    return page.get_by_test_id("statements").locator('tr[data-unit="A"]')


def _pdf_text(page, row) -> str:
    href = row.get_by_role("link", name=re.compile("PDF")).get_attribute("href")
    resp = page.request.get(href)
    assert resp.status == 200, f"{href} -> {resp.status}"
    reader = PdfReader(io.BytesIO(resp.body()))
    return " ".join(" ".join((p.extract_text() or "").split()) for p in reader.pages)


def _autopay_mail(mail):
    return [
        m
        for m in mail.messages()
        if m2.OWNERA_EMAIL.lower() in (a.lower() for a in m.to)
        and m.subject.startswith("Autopay of up to")
    ]


def _owner_sees_autopay_line(page, login, amount: str):
    """The owner's statements list shows the autopay line on the sent statement."""
    page.context.clear_cookies()
    login(m2.OWNERA_EMAIL)
    page.goto("/me/statements/")
    line = page.locator('[data-testid^="statement-autopay-"]')
    expect(line).to_have_count(1)
    expect(line).to_contain_text(f"${amount}")
    expect(line).to_contain_text(re.compile("Dec(ember)? 10"))


def test_balance_statement_is_the_notice(page, residents_on, login, mail, axe):
    _enroll_a(page, login)
    row = _generate_and_send(page, login)
    line = row.get_by_test_id("autopay-line")
    expect(line).to_have_text(re.compile(AUTOPAY_RE))
    amount = re.search(r"\$([\d,]+\.\d\d)", line.inner_text()).group(1)
    axe(page)
    pdf = _pdf_text(page, row)
    assert f"Autopay will debit up to ${amount} on December 10, 2026" in pdf, pdf[-600:]

    page.get_by_test_id("send-statements").click()
    expect(page.get_by_text(re.compile(r"Sent \d+ statements"))).to_be_visible()
    m2.drain_jobs()
    msg = mail.wait_for(m2.OWNERA_EMAIL)
    assert "statement" in msg.subject.lower(), msg.subject
    assert f"Autopay will debit up to ${amount} on December 10, 2026" in msg.body, msg.body
    _owner_sees_autopay_line(page, login, amount)

    # December's notice date: the sent statement already served, so no separate notice.
    m2.run_scheduled("2026-11-25")
    m2.drain_jobs()
    dec = [m for m in _autopay_mail(mail) if "December 10, 2026" in m.subject]
    assert not dec, [m.subject for m in dec]


def test_fixed_autopay_gets_no_notice(page, residents_on, login, mail):
    _enroll_a(page, login, fixed="100.00")
    row = _generate_and_send(page, login)
    expect(row.get_by_test_id("autopay-line")).to_have_text("Autopay up to $100.00 on Dec 10")
    pdf = _pdf_text(page, row)
    assert "Autopay will debit your fixed amount of up to $100.00 on December 10, 2026" in pdf
    page.get_by_test_id("send-statements").click()
    m2.drain_jobs()
    msg = mail.wait_for(m2.OWNERA_EMAIL)
    assert "fixed amount of up to $100.00" in msg.body, msg.body
    _owner_sees_autopay_line(page, login, "100.00")

    for day in ["2026-10-26", "2026-11-25"]:
        m2.run_scheduled(day)
        m2.drain_jobs()
    assert not _autopay_mail(mail), [m.subject for m in _autopay_mail(mail)]
