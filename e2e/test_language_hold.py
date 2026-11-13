"""Preferred-language hold on statutory notices and pre-notice late-fee caps.

Fixed clock 2026-10-07; the ladder and late fees run for later dates via run_scheduled.
Unit G's owner is Abdi Warsame (person 9 in the demo seed). Fakes only.
"""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL
from helpers import m2
from helpers import seed as seed_helpers

PDF = Path(__file__).parent / "fixtures" / "translation_es.pdf"  # synthetic, one blank page
STATUTORY_TEMPLATE = "Notice of delinquency (statutory)"


@pytest.fixture
def billed(seed):
    return seed_helpers.billing()


def _g(page):
    return page.get_by_test_id("delinquency-cases").locator('tr[data-unit="G"]')


def test_language_hold_then_translation_releases(page, billed, login, mail, axe):
    login(BOARD_EMAIL)
    page.goto("/units/G/")
    page.locator("main a[href*='/people/']").first.click()
    page.get_by_role("link", name="Edit").first.click()
    page.get_by_label("Preferred language").select_option("es")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("Person saved.")).to_be_visible()
    expect(page.locator("dd").filter(has_text="Spanish")).to_be_visible()
    # No axe here: the person page has the shared link-in-text-block contrast
    # issue also reported on /me/pay/.

    m2.run_scheduled("2026-10-10")
    page.goto("/money/delinquency/")
    row = _g(page)
    expect(row.get_by_test_id("waiting-translation")).to_have_text("Waiting for translation")
    expect(row).to_contain_text("Spanish")
    expect(row.get_by_test_id("preforeclosure-records")).to_have_count(0)
    axe(page)

    page.goto("/settings/letters/")
    page.get_by_role("link", name=STATUTORY_TEMPLATE).click()
    tr = page.locator("#translations")
    tr.get_by_label("Language").select_option("es")
    tr.get_by_label("Or the translated text").fill("AVISO DE MOROSIDAD (traduccion de prueba E2E)")
    # The template form above also has a "note" field (duplicate id_note), so the label
    # does not reach this input; fill by name inside the translations card.
    tr.locator("[name=note]").fill("E2E translator")
    tr.get_by_label("The board approves this translation").check()
    tr.get_by_role("button", name="Save translation").click()
    expect(page.locator("#translations tbody tr").filter(has_text="E2E translator")).to_have_count(
        1
    )
    axe(page)

    m2.run_scheduled("2026-10-11")
    page.goto("/money/delinquency/")
    row = _g(page)
    expect(row.get_by_test_id("waiting-translation")).to_have_count(0)
    records = row.get_by_test_id("preforeclosure-records")
    expect(records).to_have_count(1)
    expect(records.get_by_role("link", name="PDF").first).to_be_visible()
    axe(page)
    assert mail.messages() == []  # residents off: previews and draft batches only


def test_pre_notice_late_fee_capped(page, billed, login, axe):
    """WA cap before the first notice: min($50, 5% of the unpaid assessment)."""
    m2.run_scheduled("2026-10-25")
    login(BOARD_EMAIL)
    page.goto("/money/assessments/late-fees/")
    rows = page.locator("table tbody tr").filter(has_text="Late fee ·")
    a = rows.filter(has=page.locator("td", has_text="A")).filter(has_text="October 2026")
    expect(a.first).to_contain_text("limited to $19.83 before the first notice")
    expect(a.first).to_contain_text("Capped")
    # $25 policy fee is never charged inside the window.
    expect(rows.filter(has_text="$25.00")).to_have_count(0)
    # Unit G already has a fee in its window: no second (October) late fee.
    g = rows.filter(has=page.locator("td", has_text="G"))
    expect(g).to_have_count(1)
    expect(g).to_contain_text("September 2026")
    axe(page)


def test_pdf_translation_attached_to_email_copy(page, billed, login, mail):
    login(BOARD_EMAIL)
    page.goto("/units/G/")
    page.locator("main a[href*='/people/']").first.click()
    page.get_by_role("link", name="Edit").first.click()
    page.get_by_label("Preferred language").select_option("es")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page.get_by_text("Person saved.")).to_be_visible()

    page.goto("/settings/letters/")
    page.get_by_role("link", name=STATUTORY_TEMPLATE).click()
    tr = page.locator("#translations")
    tr.get_by_label("Language").select_option("es")
    tr.get_by_label("Translation (PDF)").set_input_files(str(PDF))
    tr.locator("[name=note]").fill("E2E translator")
    tr.get_by_label("The board approves this translation").check()
    tr.get_by_role("button", name="Save translation").click()
    expect(page.locator("#translations tbody tr").filter(has_text="E2E translator")).to_have_count(
        1
    )

    m2.run_scheduled("2026-10-10")
    m2.run_scheduled("2026-10-11")  # same dates as the text-translation test above
    page.goto("/money/delinquency/")
    expect(_g(page).get_by_test_id("waiting-translation")).to_have_count(0)
    expect(_g(page).get_by_test_id("preforeclosure-records")).to_have_count(1)
    assert mail.messages() == []  # preview until released

    m2.residents_enabled(True)
    page.reload()
    _g(page).get_by_test_id("send-email-copy").click()
    expect(page.get_by_text("Email copy sent to")).to_be_visible()
    m2.drain_jobs()
    msg = mail.wait_for(OWNER_EMAIL)
    assert "enclosed with the mailed notice" not in msg.body
    assert [ct for _, ct in msg.attachments] == ["application/pdf"], msg.attachments
    assert msg.attachments[0][0].endswith(".pdf"), msg.attachments
