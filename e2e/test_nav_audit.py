"""Nav audit: every menu link lands on a real page, or on a placeholder only when
no real page exists yet.

Walks the board shell (top nav, hub section tabs, phone tab bar, More sheet,
settings cog) as the treasurer, and the owner portal tabs as an owner with
residents on. Writes the table to $HOMEOA_NAV_AUDIT_OUT when set.

KNOWN_MISMATCHES lists nav links that show the placeholder although a real page
exists. The fix task removes entries as it wires them; a stale entry fails too.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2

# href -> url_name to wire (the real page that already exists).
KNOWN_MISMATCHES: dict[str, str] = {}

BOARD_NAV = 'nav.hnav a, nav.tabs a, nav.tabbar a[href^="/"], #sheet a, a[aria-label="Settings"]'
PORTAL_NAV = "nav.hnav a, nav.tabbar a"
# Portal tabs that render portal/tab.html ("coming soon") until they are built.
PORTAL_PLACEHOLDER_VIEWS = {"homeoa.portal.views.requests_tab", "homeoa.portal.views.docs"}
PLACEHOLDER_TEXT = ("This page is not built yet.", "This page is coming soon.")


def _crawl(page, start: str, selector: str, scope: str) -> list[dict]:
    rows: dict[str, dict] = {start: {"label": "(start)", "href": start, "from": "-"}}
    queue = [start]
    seen_pages: set[str] = set()
    while queue:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        resp = page.goto(url)
        status = resp.status if resp else 0
        body = page.locator("main").inner_text() if page.locator("main").count() else ""
        placeholder = any(t in body for t in PLACEHOLDER_TEXT)
        if url in rows:
            rows[url].update(status=status, placeholder=placeholder)
        if status != 200:
            continue
        for a in page.locator(selector).all():
            href = a.get_attribute("href") or ""
            path = urlparse(href).path
            if not path.startswith("/") or not path.startswith(scope):
                continue
            label = " ".join((a.text_content() or a.get_attribute("aria-label") or "").split())
            if path not in rows:
                rows[path] = {"label": label, "href": path, "from": url}
                queue.append(path)
    # start page itself is reached through the brand/home link
    return sorted(rows.values(), key=lambda r: r["href"])


def _write(rows: list[dict], who: str) -> None:
    out = os.environ.get("HOMEOA_NAV_AUDIT_OUT")
    if not out:
        return
    with open(out, "a") as f:
        f.write(f"\n### {who}\n\n| Label | URL | Status | Lands on |\n|---|---|---|---|\n")
        for r in rows:
            kind = "placeholder" if r.get("placeholder") else "real page"
            if r["href"] in KNOWN_MISMATCHES:
                kind += f" (MISMATCH: wire {KNOWN_MISMATCHES[r['href']]})"
            f.write(f"| {r['label']} | {r['href']} | {r.get('status')} | {kind} |\n")


def _check(page, rows: list[dict]) -> None:
    bad = [r for r in rows if r.get("status") != 200]
    assert not bad, f"nav links not 200: {[(r['href'], r.get('status')) for r in bad]}"
    suspects = [r for r in rows if r.get("placeholder") and r["href"] not in KNOWN_MISMATCHES]
    if suspects:
        # A placeholder is fine only when the URL is served by a placeholder view.
        hrefs = [r["href"] for r in suspects]
        code = (
            "from django.urls import resolve\n"
            f"for h in {hrefs!r}:\n"
            "    f = resolve(h).func\n"
            "    print('NAV', h, f.__module__ + '.' + f.__name__)"
        )
        out = m2._run("shell", "-c", code)
        views = dict(ln.split()[1:3] for ln in out.splitlines() if ln.startswith("NAV "))
        real = [
            (h, v)
            for h, v in views.items()
            if not (v.startswith("homeoa.ui.views.") or v in PORTAL_PLACEHOLDER_VIEWS)
        ]
        assert not real, f"placeholder shown but URL resolves to a real view: {real}"
    shown = {r["href"] for r in rows if r.get("placeholder")}
    stale = [h for h in KNOWN_MISMATCHES if h in {r["href"] for r in rows} and h not in shown]
    assert not stale, f"fixed; remove from KNOWN_MISMATCHES: {stale}"


def test_board_nav_links(page, seed, login):
    login(BOARD_EMAIL)
    rows = _crawl(page, "/", BOARD_NAV, "/")
    _write(rows, "Treasurer (board shell)")
    labels = {r["label"] for r in rows}
    for hub in ("Money", "Requests", "Community", "Governance", "Units", "Vendors", "Documents"):
        assert hub in labels, f"hub {hub} missing from nav"
    _check(page, rows)


def test_phone_more_sheet_opens(phone_page, seed, login):
    login(BOARD_EMAIL, phone_page)
    phone_page.goto("/")
    phone_page.locator("#tabMore").click()
    sheet = phone_page.locator("#sheet")
    expect(sheet).to_be_visible()
    sheet.get_by_role("link", name="Vendors").click()
    expect(phone_page).to_have_url(f"{phone_page.url.split('/vendors')[0]}/vendors/")


def test_owner_portal_tabs(page, seed, login):
    m2.residents_enabled(True)
    try:
        login(m2.OWNERC_EMAIL)
        rows = _crawl(page, "/me/", PORTAL_NAV, "/me/")
        _write(rows, "Owner (portal, residents on)")
        assert rows, "portal shows no tabs"
        _check(page, rows)
    finally:
        m2.residents_enabled(False)
