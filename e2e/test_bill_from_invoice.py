"""Money > Bills > Add bill > Fill in from invoice: the fake AI reads a
demo invoice, the form opens filled in with a "check every field" banner and nothing
saved, and submitting books the bill with the invoice attached."""

import re

from playwright.sync_api import expect
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from conftest import BOARD_EMAIL

LINES = (
    "City of Tukwila Utilities",
    "Invoice # UT-2026-10",
    "Invoice date: 2026-10-01",
    "Due: 2026-10-25",
    "For: Water and sewer, September",
    "Total due: $612.34",
)


def _invoice(path):
    c = canvas.Canvas(str(path), pagesize=LETTER)
    for i, line in enumerate(LINES):
        c.drawString(72, 700 - 18 * i, line)
    c.save()
    return str(path)


def test_fill_in_a_bill_from_an_invoice(page, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    page.goto("/money/")
    page.get_by_role("link", name="Bills", exact=True).first.click()
    page.get_by_role("link", name=re.compile(r"add bill|new bill", re.IGNORECASE)).first.click()
    reader = page.get_by_test_id("invoice-reader")
    reader.get_by_label("Invoice file (PDF or image)").set_input_files(
        _invoice(tmp_path / "october.pdf")
    )
    reader.get_by_role("button", name="Fill in from invoice").click()

    expect(page.get_by_test_id("bill-prefilled")).to_contain_text(
        "Filled in by AI from october.pdf"
    )
    expect(page.get_by_test_id("bill-held-file")).to_contain_text("october.pdf will be attached")
    expect(page.get_by_label("Invoice number")).to_have_value("UT-2026-10")
    expect(page.get_by_label("Due date")).to_have_value("2026-10-25")
    axe(page)

    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_text("Bill submitted and booked to accounts payable.")).to_be_visible()
    expect(page.get_by_text("october.pdf")).to_be_visible()
    expect(page.get_by_text("$612.34").first).to_be_visible()
