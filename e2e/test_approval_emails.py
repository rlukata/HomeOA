"""Approval emails (FIN-12): submitting a bill emails each eligible approver (not the
submitter); the final approval or a rejection emails the submitter, with the reason on a
reject. Mail is read from the E2E file mailbox, never a live provider."""

import time

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL
from helpers import m2
from test_approvals import REPAIR, THIRD_EMAIL, _submitted, rules  # noqa: F401
from test_bills import MOW


def _wait(mail, to, needle, timeout=15):
    m2.drain_jobs()  # no worker on the E2E server: send queued email now
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hits = [m for m in mail.messages() if to in m.to and needle in m.subject]
        if hits:
            return hits
        time.sleep(0.3)
    seen = [(m.to, m.subject) for m in mail.messages()]
    raise AssertionError(f"no mail to {to} with {needle!r}; saw {seen}")


def _none(mail, to, needle):
    m2.drain_jobs()
    assert not [m for m in mail.messages() if to in m.to and needle in m.subject]


def test_awaiting_then_approved(page, rules, login, mail):  # noqa: F811
    url = _submitted(page, login, "INV-F12A", (REPAIR,))
    for who in (SECRETARY_EMAIL, THIRD_EMAIL):
        (msg,) = _wait(mail, who, "needs your approval")
        assert "INV-F12A" in msg.subject + msg.body
        assert "/money/bills/" in msg.body
    _none(mail, BOARD_EMAIL, "needs your approval")  # submitter is not asked

    login(SECRETARY_EMAIL)
    page.goto(url)
    page.get_by_test_id("bill-approve").click()
    expect(page.get_by_test_id("approval-progress")).to_have_text("1 of 2 approvals")
    _none(mail, BOARD_EMAIL, "was approved")  # not final yet

    page.context.clear_cookies()
    login(THIRD_EMAIL)
    page.goto(url)
    page.get_by_test_id("bill-approve").click()
    expect(page.get_by_test_id("approval-progress")).to_have_text("Approved · pay at bank")
    (msg,) = _wait(mail, BOARD_EMAIL, "was approved")
    assert "can be paid" in msg.body


def test_rejected_with_reason(page, rules, login, mail):  # noqa: F811
    url = _submitted(page, login, "INV-F12R", (MOW,))
    _wait(mail, SECRETARY_EMAIL, "needs your approval")
    login(SECRETARY_EMAIL)
    page.goto(url)
    page.get_by_label("Reason").fill("Wrong amount")
    page.get_by_test_id("bill-reject").click()
    expect(page.get_by_text("Bill rejected and returned to draft.")).to_be_visible()
    (msg,) = _wait(mail, BOARD_EMAIL, "was rejected")
    assert "Reason: Wrong amount" in msg.body
    assert "returned it to draft" in msg.body
