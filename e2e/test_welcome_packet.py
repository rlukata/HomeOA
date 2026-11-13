"""Owners' welcome packet. The Secretary writes the message on Documents > Welcome packet;
an owner sees a Welcome card on Home, opens the packet (message, documents, board, how
things work) and the card goes away. Tenants have no packet. Synthetic seed data only."""

from playwright.sync_api import expect

from conftest import OWNER_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL
from test_portal import billed, residents_on  # noqa: F401


def test_secretary_writes_owner_reads(page, residents_on, login, axe):  # noqa: F811
    login(SECRETARY_EMAIL)
    page.goto("/documents/")
    page.get_by_role("link", name="Welcome packet").click()
    expect(page.get_by_role("heading", name="Welcome packet", level=1)).to_be_visible()
    expect(page.get_by_test_id("welcome-documents")).to_contain_text("Bylaws")
    axe(page)
    page.get_by_role("textbox", name="Welcome message").fill(
        "Hello from the board.\n\nWelcome aboard!"
    )
    page.get_by_role("button", name="Save message").click()
    expect(page.get_by_text("Welcome message saved.")).to_be_visible()
    page.context.clear_cookies()

    login(OWNER_EMAIL)
    page.goto("/me/")
    card = page.get_by_test_id("welcome-prompt")
    expect(card).to_be_visible()
    card.get_by_role("link", name="Open welcome packet").click()
    expect(page.get_by_role("heading", level=1)).to_contain_text("Welcome to")
    expect(page.get_by_test_id("welcome-message")).to_contain_text("Hello from the board.")
    expect(page.get_by_test_id("welcome-documents")).to_contain_text("Rules & regulations 2025")
    expect(page.get_by_test_id("contact-board")).to_be_visible()
    axe(page)
    page.goto("/me/")
    expect(page.get_by_test_id("welcome-prompt")).to_have_count(0)
    page.goto("/me/more/")
    expect(page.get_by_test_id("more-welcome")).to_be_visible()


def test_tenant_has_no_packet(page, residents_on, login):  # noqa: F811
    login(TENANT_EMAIL)
    page.goto("/me/")
    expect(page.get_by_test_id("welcome-prompt")).to_have_count(0)
    page.goto("/me/more/")
    expect(page.get_by_test_id("more-welcome")).to_have_count(0)
    assert page.goto("/me/welcome/").status == 403
