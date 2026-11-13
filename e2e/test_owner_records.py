"""Owner records export (REG-05). Owner mode (/units/records/?format=) leaves
out unlisted phones, confidential emails, non-owner emails and ACP addresses (substitute
or blank) in CSV, XLSX and PDF. Full mode is a POST with a reason (blank -> 400), needs
settings.manage, and is watermarked. Owners set email_confidential in the portal profile.
The resident directory (More > Resident directory) applies the same redaction; tenants
don't get it. Records requests (Documents > Records requests; owners under More) show on
Home with their due date until marked fulfilled. Synthetic seed data only."""

import csv
import io
import re

import pytest
from openpyxl import load_workbook
from playwright.sync_api import expect
from pypdf import PdfReader

from conftest import BOARD_EMAIL, OWNER_EMAIL, TENANT_EMAIL
from helpers import m2
from helpers import seed as seed_helpers
from test_portal import billed, residents_on  # noqa: F401

RECORDS = "/units/records/"
FULL = RECORDS + "full/"
PHONE = "206-555-0199"
SUBST = "PO Box 4257 Olympia WA 98504"
WATERMARK = "CONTAINS CONFIDENTIAL DATA"

_PRIVACY = f"""
import json
from homeoa.registry.models import OwnershipShare, Person
def owner_of(n):
    q = OwnershipShare.all_associations.filter(period__unit__number=n, period__end__isnull=True)
    return q.order_by("pk").last().person
c = Person.all_associations.get(email={m2.OWNERC_EMAIL!r})
c.phone = {PHONE!r}; c.phone_unlisted = True; c.save()
a = Person.all_associations.get(email={m2.OWNERA_EMAIL!r})
a.acp_participant = True; a.acp_address = {SUBST!r}; a.save()
d = owner_of("D")
d.address_line1 = "77 Hidden Lane"; d.acp_participant = True; d.acp_address = ""; d.save()
print("E2EJSON" + json.dumps({{"d_name": d.full_name}}))
"""


@pytest.fixture
def privacy(residents_on):  # noqa: F811
    return seed_helpers.run_script(_PRIVACY, "privacy")


def _text(fmt: str, body: bytes) -> str:
    if fmt == "csv":
        return body.decode("utf-8-sig")
    if fmt == "xlsx":
        ws = load_workbook(io.BytesIO(body), read_only=True).active
        return "\n".join(
            ",".join("" if c is None else str(c) for c in r) for r in ws.iter_rows(values_only=True)
        )
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(body)).pages)


def _csv_rows(text):
    return list(csv.reader(io.StringIO(text)))


def _set_email_confidential(page, login):
    login(OWNER_EMAIL)
    page.goto("/me/profile/")
    box = page.get_by_label(re.compile("keep email confidential", re.I))
    box.check()
    page.get_by_role("button", name="Save contact details").click()
    page.goto("/me/profile/")
    expect(page.get_by_label(re.compile("keep email confidential", re.I))).to_be_checked()
    page.context.clear_cookies()


def test_owner_mode_redacts_every_format(page, privacy, login, axe):
    _set_email_confidential(page, login)
    login(BOARD_EMAIL)
    page.goto(RECORDS)
    expect(page.get_by_test_id("owner-export")).to_be_visible()
    axe(page)
    for fmt in ("csv", "xlsx", "pdf"):
        link = page.get_by_test_id("owner-export").locator(f'a[data-export="{fmt}"]')
        with page.expect_download() as dl:
            link.click()
        assert re.match(
            rf"^[A-Za-z0-9-]+-owner-records-\d{{4}}-\d\d-\d\d\.{fmt}$", dl.value.suggested_filename
        ), dl.value.suggested_filename
        resp = page.request.get(RECORDS + f"?format={fmt}")
        assert resp.status == 200
        text = _text(fmt, resp.body())
        flat = re.sub(r"\s+", " ", text)
        assert OWNER_EMAIL not in text, fmt  # owner chose confidential email
        assert TENANT_EMAIL not in text, fmt  # non-owner email
        assert PHONE not in text, fmt  # unlisted phone
        assert "77 Hidden Lane" not in flat, fmt  # ACP, no substitute: blank
        assert SUBST not in flat and "Address withheld" in flat, fmt  # ACP: removed
        assert m2.OWNERC_EMAIL in text, fmt  # an ordinary owner email stays
        assert WATERMARK not in text, fmt
    rows = _csv_rows(page.request.get(RECORDS + "?format=csv").body().decode("utf-8-sig"))
    hdr = rows[0]
    d_rows = [r for r in rows if privacy["d_name"] in r]
    assert d_rows and d_rows[0][hdr.index("Mailing address")] == "Address withheld", d_rows


def _full(page, fmt, reason):
    token = page.locator("[data-testid=full-export] input[name=csrfmiddlewaretoken]")
    return page.request.post(
        FULL,
        form={"csrfmiddlewaretoken": token.get_attribute("value"), "format": fmt, "reason": reason},
        headers={"Referer": page.url},
    )


def test_full_mode_reason_and_watermark(page, privacy, login):
    login(BOARD_EMAIL)
    page.goto(RECORDS)
    expect(page.get_by_test_id("full-export")).to_be_visible()
    assert _full(page, "csv", "   ").status == 400
    for fmt in ("csv", "xlsx", "pdf"):
        resp = _full(page, fmt, "Hand-over to new manager")
        assert resp.status == 200, (fmt, resp.status)
        cd = resp.headers["content-disposition"]
        assert "owner-records-full-confidential" in cd, cd
        body = resp.body()
        if fmt == "pdf":
            assert body.startswith(b"%PDF")
            continue  # the watermark is drawn on the canvas, not extractable reliably
        text = _text(fmt, body)
        assert WATERMARK in text, fmt
        assert TENANT_EMAIL in text and PHONE in text and "77 Hidden Lane" in text, fmt
    # Via the form too: the browser download carries the watermark.
    form = page.get_by_test_id("full-export")
    form.get_by_label("Format").select_option("csv")
    form.get_by_label("Reason").fill("Audit check")
    with page.expect_download() as dl:
        form.get_by_role("button", name="Download full export").click()
    with open(dl.value.path(), encoding="utf-8-sig") as fh:
        assert WATERMARK in fh.read()


def test_owner_cannot_export(page, privacy, login):
    login(OWNER_EMAIL)
    assert page.goto(RECORDS).status in (403, 404)
    assert page.goto(RECORDS + "?format=csv").status in (403, 404)
    resp = page.request.post(FULL, form={"format": "csv", "reason": "x"})
    assert resp.status in (403, 404), resp.status


def test_records_request_on_home(page, privacy, login):
    login(BOARD_EMAIL)
    page.goto("/documents/")
    page.get_by_role("link", name="Records requests").click()
    page.get_by_label(re.compile(r"Or name", re.I)).fill("Pat Requester")
    page.get_by_label("Received on").fill("2026-10-05")
    page.get_by_label("What was asked for").fill("Minutes 2025")
    page.get_by_role("button", name="Log request").click()
    msg = page.get_by_text(re.compile(r"Records request logged\. Due (\d{4}-\d\d-\d\d)"))
    expect(msg).to_be_visible()
    due = re.search(r"\d{4}-\d\d-\d\d", msg.inner_text()).group(0)
    page.goto("/")
    todo = page.locator('[id^="todo-records-request-"]')
    expect(todo).to_have_count(1)
    expect(todo).to_contain_text("Pat Requester")
    expect(todo).to_contain_text(due)
    todo.get_by_role("link").first.click()
    page.get_by_test_id("records-requests").get_by_role("button", name="Mark fulfilled").click()
    page.goto("/")
    expect(page.locator('[id^="todo-records-request-"]')).to_have_count(0)


def test_owner_asks_for_records_and_sees_it_fulfilled(page, privacy, login, axe):
    login(OWNER_EMAIL)
    page.goto("/me/more/")
    page.get_by_test_id("more-records").click()
    expect(page.get_by_role("heading", name="Records requests", level=1)).to_be_visible()
    axe(page)
    page.get_by_label("Which records?").fill("Bank statements for 2025")
    page.get_by_label("Inspect in person").check()
    page.get_by_role("button", name="Send request").click()
    expect(page.get_by_text(re.compile(r"Request sent to the board\."))).to_be_visible()
    mine = page.get_by_test_id("my-records-requests")
    expect(mine).to_contain_text("Bank statements for 2025")
    expect(mine).to_contain_text("Open")
    page.context.clear_cookies()

    login(BOARD_EMAIL)
    page.goto("/documents/")
    page.get_by_role("link", name="Records requests").click()
    rows = page.get_by_test_id("records-requests")
    expect(rows).to_contain_text("Inspect in person")
    axe(page)
    rows.get_by_role("button", name="Mark fulfilled").click()
    page.context.clear_cookies()

    login(OWNER_EMAIL)
    page.goto("/me/records/")
    expect(page.get_by_test_id("my-records-requests")).to_contain_text("Fulfilled")


def test_resident_directory_uses_the_same_redaction(page, privacy, login, axe):
    login(OWNER_EMAIL)
    page.goto("/me/more/")
    page.get_by_test_id("more-directory").click()
    expect(page.get_by_role("heading", name="Resident directory", level=1)).to_be_visible()
    expect(page.get_by_test_id("directory-unit")).to_have_count(9)
    axe(page)
    text = re.sub(r"\s+", " ", page.locator("main").inner_text())
    assert PHONE not in text  # unlisted phone
    assert TENANT_EMAIL not in text  # non-owner email
    assert "77 Hidden Lane" not in text  # ACP, no substitute
    assert SUBST not in text and "Address withheld" in text  # ACP: removed
    assert m2.OWNERC_EMAIL in text
    page.context.clear_cookies()
    login(TENANT_EMAIL)
    page.goto("/me/more/")
    expect(page.get_by_test_id("more-directory")).to_have_count(0)
    assert page.goto("/me/directory/").status == 403
