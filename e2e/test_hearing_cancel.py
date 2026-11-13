"""Cancelling a hearing sends a "Hearing cancelled" letter (step on the violation,
comm log entry, email copy only to owners with e-notice consent); rescheduling sends an
updated notice with the new date. Unit A has consent in the seed; unit I (president)
has an email on file but no consent, so his letter goes to the mail batch only."""

import io
import re

from playwright.sync_api import expect
from pypdf import PdfReader

from conftest import BOARD_EMAIL
from helpers import m2, m5
from test_hearings import _schedule_hearing
from test_violations import _category, _fill_log, _schedule

REASON = "Owner asked to move"


def _open(page, tmp_path, unit):
    _category(page)
    _schedule(page, furnished="2026-08-25")
    _fill_log(page, m5.png(tmp_path, "bins.png"), unit)
    expect(page.get_by_text("Violation logged.")).to_be_visible()
    page.get_by_test_id("send-courtesy").click()
    page.get_by_test_id("send-formal").click()
    expect(page.locator("tr[data-kind=formal]")).to_be_visible()
    return page.url


def _cancel(page):
    page.get_by_test_id("hearing-change").locator("summary").click()
    page.get_by_test_id("cancel-form").get_by_label("Reason").fill(REASON)
    page.get_by_test_id("cancel-hearing").click()
    expect(page.get_by_text("Hearing cancelled.")).to_be_visible()


def _pdf_text(page, row) -> str:
    href = row.get_by_role("link", name="PDF").get_attribute("href")
    resp = page.request.get(href)
    assert resp.status == 200, f"{href} -> {resp.status}"
    reader = PdfReader(io.BytesIO(resp.body()))
    return " ".join(" ".join((p.extract_text() or "").split()) for p in reader.pages)


def _cancel_mails(mail, unit):
    return [m for m in mail.messages() if m.subject == f"Hearing cancelled: unit {unit}"]


def test_cancel_emails_consenting_owner(page, seed, login, axe, tmp_path, mail):
    m2.residents_enabled(True)  # Owner email is only a preview while residents are off
    login(BOARD_EMAIL)
    url = _open(page, tmp_path, "A")
    _schedule_hearing(page)
    _cancel(page)
    row = page.locator("tr[data-kind=hearing_cancelled]")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Hearing cancelled")
    expect(row).to_contain_text("Email")
    text = _pdf_text(page, row)
    assert "has been cancelled" in text and REASON in text, text[:600]
    axe(page)

    m2.drain_jobs()
    msg = mail.wait_for(m2.OWNERA_EMAIL)
    sent = _cancel_mails(mail, "A")
    assert [m.to for m in sent] == [[m2.OWNERA_EMAIL]], [(m.subject, m.to) for m in sent]
    assert any(ct == "application/pdf" for _n, ct in sent[0].attachments), msg.subject

    page.goto("/units/A/comms/")
    expect(page.get_by_test_id("comm-log")).to_contain_text("Hearing cancelled")

    # A new hearing can be scheduled after the cancellation.
    page.goto(url)
    _schedule_hearing(page)
    expect(page.get_by_test_id("hearing-cancelled")).to_have_count(1)
    expect(page.locator("tr[data-kind=hearing_notice]")).to_have_count(2)
    expect(page.get_by_test_id("hearing-change")).to_be_visible()


def test_cancel_without_consent_is_letter_only(page, seed, login, tmp_path, mail):
    m2.residents_enabled(True)
    login(BOARD_EMAIL)
    _open(page, tmp_path, "I")
    _schedule_hearing(page)
    _cancel(page)
    row = page.locator("tr[data-kind=hearing_cancelled]")
    expect(row).to_contain_text("Letter")
    expect(row.get_by_role("link", name="mail batch")).to_be_visible()
    m2.drain_jobs()
    assert _cancel_mails(mail, "I") == [], "no-consent owner got an email copy"
    page.goto("/units/I/comms/")
    expect(page.get_by_test_id("comm-log")).to_contain_text("Hearing cancelled")


def test_reschedule_notice_shows_new_date(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _open(page, tmp_path, "I")
    _schedule_hearing(page)
    page.get_by_test_id("hearing-change").locator("summary").click()
    form = page.get_by_test_id("reschedule-form")
    form.locator("input[name=hearing_on]").fill("2026-10-28")
    form.locator("input[name=place]").fill("Community room")
    form.get_by_test_id("reschedule-hearing").click()
    expect(page.get_by_text("Hearing rescheduled and updated notice sent.")).to_be_visible()
    rows = page.locator("tr[data-kind=hearing_notice]")
    expect(rows).to_have_count(2)
    texts = [_pdf_text(page, rows.nth(i)) for i in range(2)]
    new = [t for t in texts if re.search(r"October 28, 2026", t)]
    assert len(new) == 1, texts
    assert "Community room" in new[0], new[0][:600]
    assert "October 21, 2026" not in new[0], new[0][:600]
