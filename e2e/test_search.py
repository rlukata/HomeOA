"""Search everything from the top bar (PLT-13, DOC-05): a misspelled owner name
finds the person; the text inside an uploaded file finds the document with the matching
words marked; an owner finds only documents owners may see."""

import re

from playwright.sync_api import expect

from conftest import OWNER_EMAIL, SECRETARY_EMAIL
from helpers import m2


def _search(page, query):
    page.goto("/")
    page.get_by_role("link", name="Search", exact=True).click()
    page.get_by_label("Search for").fill(query)
    page.get_by_role("button", name="Search", exact=True).click()


def _upload_text(page, tmp_path, title, body, visibility):
    path = tmp_path / f"{re.sub(r'\W+', '-', title.lower())}.txt"
    path.write_text(body)
    page.goto("/documents/upload/")
    page.get_by_label("Files").set_input_files(str(path))
    page.get_by_label("Category").select_option(label="Governing")
    page.get_by_label("Visible to").select_option(label=visibility)
    page.get_by_label("Title").fill(title)
    page.get_by_role("button", name="Upload", exact=True).click()


def test_search_people_and_text_inside_documents(
    page, browser, base_url, seed, login, axe, tmp_path
):
    login(SECRETARY_EMAIL)
    _upload_text(page, tmp_path, "E2E Trash rules", "Bins go out on Tuesday nights.", "Owners")
    _upload_text(page, tmp_path, "E2E Legal memo", "Bins dispute with unit G.", "Board only")

    _search(page, "Nguyn")
    hit = page.get_by_test_id("search-hit").filter(has_text="Maya Nguyen")
    expect(hit).to_be_visible()
    axe(page)

    _search(page, "tuesday bins")
    doc = page.get_by_test_id("search-hit").filter(has_text="E2E Trash rules")
    expect(doc.get_by_test_id("search-snippet")).to_contain_text("Tuesday")
    expect(doc.locator("mark").first).to_be_visible()
    doc.get_by_role("link", name="E2E Trash rules").click()
    expect(page.get_by_role("heading", name="E2E Trash rules")).to_be_visible()

    # An owner finds the owners' document but never the board-only one.
    m2.residents_enabled(True)
    ctx = browser.new_context(base_url=base_url)
    owner = ctx.new_page()
    login(OWNER_EMAIL, owner)
    owner.goto("/search/?q=bins")
    expect(owner.get_by_test_id("search-hit").filter(has_text="E2E Trash rules")).to_be_visible()
    expect(owner.get_by_test_id("search-hit").filter(has_text="E2E Legal memo")).to_have_count(0)
    ctx.close()
