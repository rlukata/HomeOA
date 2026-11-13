"""Upload a board-only doc, an owner doc and a tenant-visible
doc -> the owner sees only what owners may see, the tenant only tenant-visible docs; a
hidden doc's direct download is 404 (login page when signed out); a new version of a
governing doc becomes current and the old version downloads for the board only."""

import re

from playwright.sync_api import expect

from conftest import OWNER_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL
from helpers import m2

BOARD_DOC = "E2E Legal memo (board)"
OWNER_DOC = "E2E 2027 Budget (owners)"
TENANT_DOC = "E2E Pet rules (owners and tenants)"


def _pdf(path, text: str):
    body = f"%PDF-1.4\n% {text}\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    path.write_bytes(body.encode())
    return str(path)


def _upload(page, tmp_path, title, category, visibility, kind="") -> str:
    page.goto("/documents/upload/")
    slug = re.sub(r"\W+", "-", title.lower())
    page.get_by_label("Files").set_input_files(_pdf(tmp_path / f"{slug}.pdf", title))
    page.get_by_label("Category").select_option(label=category)
    page.get_by_label("Visible to").select_option(label=visibility)
    page.get_by_label("Title").fill(title)
    if kind:
        page.get_by_label("Governing document kind").select_option(label=kind)
    page.get_by_role("button", name="Upload", exact=True).click()
    page.goto("/documents/")
    page.get_by_role("link", name=title, exact=True).click()
    expect(page.get_by_test_id("doc-visibility")).to_have_text(visibility)
    return page.url  # /documents/<uuid>/


def _ctx(browser, base_url, login, email):
    ctx = browser.new_context(base_url=base_url)
    p = ctx.new_page()
    login(email, p)
    return ctx, p


def test_doc_visibility(page, browser, base_url, seed, login, axe, tmp_path):
    m2.residents_enabled(True)  # Residents switched on for the portal
    login(SECRETARY_EMAIL)
    board_url = _upload(page, tmp_path, BOARD_DOC, "Contracts", "Board only")
    owner_url = _upload(page, tmp_path, OWNER_DOC, "Financial", "Owners")
    tenant_url = _upload(page, tmp_path, TENANT_DOC, "Governing", "Owners & tenants", "Rules")
    page.goto("/documents/")
    for t in (BOARD_DOC, OWNER_DOC, TENANT_DOC):
        expect(page.get_by_role("link", name=t, exact=True)).to_be_visible()
    axe(page)

    ctx, owner = _ctx(browser, base_url, login, OWNER_EMAIL)
    owner.goto("/me/docs/")
    docs = owner.get_by_test_id("docs-list")
    expect(docs).to_contain_text(OWNER_DOC)
    expect(docs).to_contain_text(TENANT_DOC)
    expect(docs).not_to_contain_text(BOARD_DOC)
    axe(owner)
    r = owner.request.get(board_url + "download/", max_redirects=0)
    assert r.status == 404, r.status
    assert owner.request.get(owner_url + "download/").ok
    ctx.close()

    ctx, tenant = _ctx(browser, base_url, login, TENANT_EMAIL)
    tenant.goto("/me/docs/")
    docs = tenant.get_by_test_id("docs-list")
    expect(docs).to_contain_text(TENANT_DOC)
    expect(docs).not_to_contain_text(OWNER_DOC)
    expect(docs).not_to_contain_text(BOARD_DOC)
    assert tenant.request.get(owner_url + "download/", max_redirects=0).status == 404
    assert tenant.request.get(board_url + "download/", max_redirects=0).status == 404
    assert tenant.request.get(tenant_url + "download/").ok
    ctx.close()

    anon = browser.new_context(base_url=base_url)
    r = anon.request.get(board_url + "download/", max_redirects=0)
    assert r.status == 302 and "/login" in r.headers["location"], (r.status, r.headers)
    ap = anon.new_page()
    ap.goto(owner_url + "download/")
    assert "/login" in ap.url, ap.url
    anon.close()

    # A new version of the governing doc becomes current; v1 is board-only.
    page.goto(tenant_url)
    page.locator("form[action$='/versions/'] input[type=file]").set_input_files(
        _pdf(tmp_path / "pet-rules-v2.pdf", "Pet rules v2")
    )
    page.locator("form[action$='/versions/']").get_by_label("Note").fill(
        "Amendment adopted by owner vote"
    )
    page.get_by_role("button", name="Upload version").click()
    page.goto(tenant_url)
    versions = page.get_by_test_id("doc-versions")
    expect(versions.locator("tr", has_text="Current")).to_contain_text("pet-rules-v2.pdf")
    expect(versions.locator("tbody tr")).to_have_count(2)
    v1 = tenant_url + "v1/download/"
    assert page.request.get(v1).ok  # board
    assert b"Pet rules v2" in page.request.get(tenant_url + "download/").body()

    ctx, owner = _ctx(browser, base_url, login, OWNER_EMAIL)
    assert owner.request.get(v1, max_redirects=0).status == 404
    current = owner.request.get(tenant_url + "download/")
    assert current.ok and b"Pet rules v2" in current.body()
    ctx.close()
