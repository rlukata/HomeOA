"""The Secretary sends minutes for signature to the treasurer -> the
treasurer signs through the emailed single-use link -> the signed PDF is the original plus
a signature certificate (who, when, IP, hash) and its SHA-256 matches the history."""

import hashlib
import io
import re

from playwright.sync_api import expect
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from conftest import BOARD_EMAIL, SECRETARY_EMAIL
from helpers import m2

TITLE = "Minutes: Board meeting Sep 2026"


def _minutes_pdf(path) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Cedar Row Condominium - Board meeting minutes (synthetic)")
    c.showPage()
    c.save()
    data = buf.getvalue()
    path.write_bytes(data)
    return data


def _text(data: bytes) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def test_click_to_sign(page, browser, base_url, seed, login, mail, axe, tmp_path):
    m2.residents_enabled(True)  # Real sends only when switched on
    original = _minutes_pdf(tmp_path / "minutes.pdf")
    login(SECRETARY_EMAIL)
    page.goto("/governance/signatures/new/")
    page.get_by_label("Title").fill(TITLE)
    page.get_by_label("PDF to sign").set_input_files(str(tmp_path / "minutes.pdf"))
    page.get_by_label("Name 1", exact=True).fill("Tessa Treasurer")
    page.get_by_label("Email 1", exact=True).fill(BOARD_EMAIL)
    axe(page)
    page.get_by_role("button", name="Save draft").click()
    expect(page.get_by_test_id("esign-status")).to_have_text("Draft")
    detail_url = page.url
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.get_by_text("Sent for signature.")).to_be_visible()

    m2.drain_jobs()
    link = mail.wait_for(BOARD_EMAIL).first_link(re.compile(r"https?://\S+/sign/[^/\s]+/"))

    # The treasurer has an account, so the link asks them to sign in first.
    ctx = browser.new_context(base_url=base_url)
    sp = ctx.new_page()
    login(BOARD_EMAIL, sp)
    sp.goto(link)
    expect(sp.get_by_test_id("esign-consent")).to_be_visible()
    axe(sp)
    sp.get_by_label("I agree to sign electronically.").check()
    sp.get_by_label("Type your full name").fill("Tessa Treasurer")
    sp.get_by_role("button", name="Sign", exact=True).click()
    expect(sp.get_by_test_id("signed")).to_be_visible()
    again = sp.goto(link)  # a used link is rejected
    assert again.status == 410, again.status
    expect(sp.get_by_test_id("signed")).to_have_count(0)
    expect(sp.get_by_test_id("esign-consent")).to_have_count(0)
    ctx.close()

    page.goto(detail_url)
    expect(page.get_by_test_id("esign-status")).to_have_text("Completed")
    expect(page.get_by_test_id("esign-signers")).to_contain_text("Signed")
    sha = page.get_by_test_id("result-sha").inner_text().strip()
    href = page.get_by_test_id("signed-pdf").get_attribute("href")
    resp = page.request.get(href)
    assert resp.ok, resp.status
    signed = resp.body()
    assert signed.startswith(b"%PDF")
    assert hashlib.sha256(signed).hexdigest() == sha

    reader = PdfReader(io.BytesIO(signed))
    assert len(reader.pages) == len(PdfReader(io.BytesIO(original)).pages) + 1
    cert = reader.pages[-1].extract_text()
    flat = re.sub(r"\s+", "", cert)
    assert "Signature certificate" in cert
    assert TITLE in cert
    assert "Tessa Treasurer" in cert and BOARD_EMAIL in flat  # who
    assert re.search(r"\d{4}-\d\d-\d\d\d\d:\d\d:\d\dUTC", flat)  # when
    assert re.search(r"\b(127\.0\.0\.1|::1)\b", cert)  # IP
    assert hashlib.sha256(original).hexdigest() in flat  # hash of the original
    assert "synthetic" in _text(signed)  # original page kept

    # The board's Home shows a review to-do; "Mark reviewed" clears it.
    page.goto("/")
    row = page.locator("li", has_text=f"Signed: {TITLE}")
    expect(row).to_have_count(1)
    with page.expect_response(re.compile(r"/todos/esign-review-\d+/act/")) as r:
        row.get_by_role("button", name="Mark reviewed").click()
    assert r.value.ok, r.value.status
    expect(page.get_by_text("could not be done from Home")).to_have_count(0)
    expect(row).to_have_count(0)
    page.goto("/")
    expect(page.get_by_text(f"Signed: {TITLE}")).to_have_count(0)
