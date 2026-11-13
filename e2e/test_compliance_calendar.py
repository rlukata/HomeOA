"""Compliance calendar. The board adds an item from the annual report
suggestion due Dec 1 -> compliance.reminders sends one reminder per offset (30, 7) and
none on rerun; marking it done moves next_due to Dec 1 next year; the item is in the
board iCal feed and not the owner's; an overdue item is a red to-do on Home."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, manage
from helpers import m2
from helpers import seed as seed_helpers

INDEX = "/governance/compliance/"
OVERDUE = "E2E Elevator permit"

_REMINDERS = """
import json
from homeoa.notify.models import Notification
rows = Notification.all_associations.filter(
    type_key="compliance.reminder", object_key__contains="compliance-item-%d-2026-12-01"
)
print("E2EJSON" + json.dumps({"n": rows.count(), "periods": sorted({r.period for r in rows})}))
"""


def _reminders(item_pk: int) -> dict:
    return seed_helpers.run_script(_REMINDERS % item_pk, "reminders")


def _run_job(as_of: str, rerun: bool = False) -> None:
    args = ["run_scheduled", "--as-of", as_of, "--job", "compliance.reminders"]
    if rerun:
        args.append("--rerun")
    r = manage(*args, check=False)
    assert r.returncode == 0, r.stderr[-1500:]
    m2.drain_jobs()


def _feed(page, browser, path: str) -> str:
    page.goto(path)
    if page.get_by_test_id("ical-create").count():
        page.get_by_test_id("ical-create").click()
    url = page.get_by_test_id("ical-url").input_value()
    anon = browser.new_context()
    r = anon.request.get(url)
    body = r.text()
    anon.close()
    assert r.ok, r.status
    return body


def test_compliance_calendar(page, browser, base_url, seed, login, axe):
    m2.residents_enabled(True)  # The owner's community page
    login(BOARD_EMAIL)
    page.goto(INDEX)
    page.get_by_role("link", name="State annual report").click()
    expect(page.get_by_label("Title")).to_have_value("State annual report")
    page.get_by_label("First due date").fill("2026-12-01")
    page.get_by_role("button", name="Save", exact=True).click()
    expect(page).to_have_url(re.compile(r"/governance/compliance/$"))
    items = page.get_by_test_id("compliance-items")
    expect(items).to_contain_text("State annual report")
    expect(items).to_contain_text("Dec 1, 2026")
    item_href = items.get_by_role("link", name="State annual report").get_attribute("href")
    item_pk = int(re.search(r"/items/(\d+)/", item_href).group(1))

    # Reminders: 30 days before, then 7 days before; reruns send nothing new.
    _run_job("2026-11-01")
    first = _reminders(item_pk)
    assert first["periods"] == ["2026-12-01:30"], first
    _run_job("2026-11-01", rerun=True)
    assert _reminders(item_pk) == first
    _run_job("2026-11-24")
    second = _reminders(item_pk)
    assert second["periods"] == ["2026-12-01:30", "2026-12-01:7"], second
    assert second["n"] == 2 * first["n"], second
    _run_job("2026-11-24", rerun=True)
    assert _reminders(item_pk) == second

    # Feeds: in the board's, not in the owner's.
    board_feed = _feed(page, browser, "/community/calendar/")
    assert "State annual report" in board_feed, board_feed[:800]
    ctx = browser.new_context(base_url=base_url)
    op = ctx.new_page()
    login(OWNER_EMAIL, op)
    owner_feed = _feed(op, browser, "/community/me/")
    assert "BEGIN:VCALENDAR" in owner_feed
    assert "State annual report" not in owner_feed
    ctx.close()

    # Item page + mark done -> next year.
    page.goto(item_href)
    axe(page)
    page.get_by_role("button", name="Mark this occurrence done").click()
    page.goto(item_href)
    expect(page.locator(".hero")).to_contain_text("Next due Dec 1, 2027")

    # Overdue item -> red to-do on Home.
    page.goto(INDEX + "items/add/")
    page.get_by_label("Title").fill(OVERDUE)
    page.get_by_label("First due date").fill("2026-09-01")
    page.get_by_label("Repeats").select_option(label="Once")
    page.get_by_role("button", name="Save", exact=True).click()
    page.goto(INDEX)
    expect(page.get_by_test_id("compliance-items")).to_contain_text("Overdue")
    axe(page)
    page.goto("/")
    row = page.locator("li.att", has_text=f"Overdue: {OVERDUE}")
    expect(row).to_be_visible()
    expect(row.locator(".att-ic")).to_have_class(re.compile(r"\benforce\b"))
    color = row.locator(".att-ic").evaluate("e => getComputedStyle(e).color")
    danger = page.evaluate(
        "() => { const d = document.createElement('div'); d.style.color = 'var(--danger)';"
        " document.body.append(d); const c = getComputedStyle(d).color; d.remove(); return c; }"
    )
    assert color == danger, (color, danger)
