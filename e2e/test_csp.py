"""Pages and widgets changed for the enforced CSP. The conftest `csp_guard`
fails any test that sees a securitypolicyviolation or console CSP error; these
tests visit the touched flows and check the replacements for inline code work."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import m2
from test_bills import MOW, _draft

OWNER_PAGES = [
    "/me/",
    "/me/pay/",
    "/me/pay/autopay/",
    "/me/requests/",
    "/me/profile/",
    "/community/me/",
]
BOARD_PAGES = [
    "/",
    "/units/",
    "/units/C/",
    "/units/people/",
    "/settings/roles/",
    "/settings/roles/status/",
]


def test_owner_pages_render_under_csp(page, seed, login):
    m2.residents_enabled(True)
    login(OWNER_EMAIL)
    for path in OWNER_PAGES:
        resp = page.goto(path)
        assert resp.ok, (path, resp.status)
        page.wait_for_load_state("networkidle")


def test_board_pages_render_under_csp(page, seed, login):
    login(BOARD_EMAIL)
    for path in BOARD_PAGES:
        resp = page.goto(path)
        assert resp.ok, (path, resp.status)
        page.wait_for_load_state("networkidle")


def test_ical_url_selects_on_click(page, seed, login):
    m2.residents_enabled(True)
    login(OWNER_EMAIL)
    page.goto("/community/me/")
    if page.get_by_test_id("ical-create").count():
        page.get_by_test_id("ical-create").click()
    field = page.get_by_test_id("ical-url")
    field.click()
    value = field.input_value()
    assert value
    assert (
        page.evaluate(
            "() => window.getSelection().toString() || "
            "document.activeElement.value.substring("
            "document.activeElement.selectionStart, document.activeElement.selectionEnd)"
        )
        == value
    )


def test_404_page_sends_csp(page, seed, login):
    login(BOARD_EMAIL)
    resp = page.goto("/no-such-page-e2e/")
    assert resp.status == 404
    assert resp.headers.get("content-security-policy")
    page.wait_for_load_state("networkidle")


def test_stripe_bank_page(page, seed, login):
    m2.residents_enabled(True)
    login(m2.OWNERC_EMAIL)
    resp = page.goto("/plugins/stripe/bank/?unit=C")
    assert resp.ok, resp.status
    expect(page.get_by_test_id("bank-collect")).to_be_visible()
    page.wait_for_load_state("networkidle")


def test_bill_pdf_preview_iframe_loads(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")

    _draft(page, invoice="INV-CSP-1", lines=(MOW,))
    page.get_by_label("Invoice file (PDF or image)").set_input_files(str(pdf))
    page.get_by_role("button", name="Attach").click()
    expect(page.get_by_text("File attached.")).to_be_visible()
    frame = page.locator("iframe.attach-preview")
    expect(frame).to_be_visible()
    src = frame.get_attribute("src")
    r = page.request.get(src)
    assert r.ok and r.headers["content-type"].startswith("application/pdf")
    page.wait_for_timeout(1000)  # let the frame load; the guard catches blocks


def test_record_payment_htmx_under_csp(page, seed, login):
    login(BOARD_EMAIL)
    page.goto("/money/payments/new/")
    with page.expect_response(lambda r: "summary" in r.url):
        page.locator("#id_unit").select_option(label="Unit G")
    expect(page.locator("#unit-summary")).not_to_be_empty()
    page.locator("#id_amount").fill("10.00")
    page.locator("#id_received_date").fill("2026-10-07")
    page.locator("#id_method").select_option(label="Check")
    page.locator("#id_reference").fill("CSP-1")
    with page.expect_response(lambda r: r.request.method == "POST") as info:
        page.get_by_role("button", name="Record payment").click()
    assert info.value.status < 400, info.value.status
    page.wait_for_load_state("networkidle")
