"""Monthly board packet. The Treasurer opens it from Money > Reports, downloads the PDF
for September and files and emails it; it shows under Documents, board only."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL


def test_treasurer_downloads_and_files_the_packet(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/money/reports/")
    page.get_by_test_id("board-packet-link").click()
    expect(page.get_by_role("heading", name="Monthly board packet", level=1)).to_be_visible()
    page.get_by_test_id("packet-month").select_option("2026-09")
    page.get_by_role("button", name="Show").click()
    axe(page)
    with page.expect_download() as dl:
        page.get_by_test_id("packet-download").click()
    assert dl.value.suggested_filename == "board-packet-2026-09.pdf"
    page.get_by_test_id("packet-send").click()
    expect(
        page.get_by_text("Board packet filed under Documents and emailed to the board.")
    ).to_be_visible()
    page.goto("/documents/")
    expect(page.get_by_role("link", name="Board packet · September 2026")).to_be_visible()
