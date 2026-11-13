"""Resident Requests tab (POR-04) and the complete unit file (DOC-02).

/me/requests/: "New request" chooser plus one newest-first list of the resident's ARC
(owners only) and maintenance requests; tenants see maintenance only. Board
preview-as-unit shows the tab read-only. Unit file gains violations, ARC,
maintenance and work-order entries for the board; the ARC committee and the CPA have no
unit file access (no units.read). Owner7 = unit G, tenant chris.doyle = unit E."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, TENANT_EMAIL
from helpers import m2, m5
from helpers.flows import propose_role
from helpers.seed import run_script
from test_violations import _category, _log

ARC_T = "Add window boxes"
MNT_T = "Hall light flickers"
COMMITTEE_EMAIL = "arc.member@cedarrow.example"
CPA_EMAIL = "cpa@ledgerworks.example"

_BACKDATE = """
import datetime as dt, json
from django.utils import timezone
from homeoa.arc.models import ArcRequest
from homeoa.maintenance.models import MaintenanceRequest
ArcRequest.all_associations.filter(title=ARC).update(submitted_on=dt.date(*A))
MaintenanceRequest.all_associations.filter(title=MNT).update(
    created_at=timezone.make_aware(dt.datetime(*M, 12)))
print("E2EJSON" + json.dumps({"ok": True}))
"""


def _backdate(arc: tuple, mnt: tuple) -> None:
    s = (
        _BACKDATE.replace("ARC", repr(ARC_T), 1)
        .replace("MNT", repr(MNT_T), 1)
        .replace("*A", "*" + repr(arc))
        .replace("*M", "*" + repr(mnt))
    )
    run_script(s, "backdate requests")


def _titles(page) -> list[str]:
    return page.get_by_test_id("req-list").locator("[data-kind] b").all_inner_texts()


def _owner_submits(page, tmp_path):
    page.goto("/me/arc/new/")
    page.get_by_label("Short title").fill(ARC_T)
    page.get_by_label("Describe the change").fill("Cedar boxes under front windows")
    page.get_by_label("Where on the property").fill("Front windows")
    page.get_by_label("Type of change").select_option(label="Windows or doors")
    page.get_by_role("button", name="Send to the board").click()
    expect(page).to_have_url(re.compile(r"/me/arc/\d+/$"))
    page.goto("/me/maintenance/new/")
    page.get_by_label("Where is it?").fill("Second floor hall")
    page.get_by_label("What needs fixing?").fill(MNT_T)
    page.get_by_label("Details").fill("Flickers at night")
    page.get_by_label("Photos").set_input_files(m5.png(tmp_path, "hall.png"))
    page.get_by_test_id("me-maint-submit").click()
    expect(page.get_by_text("Thanks. The board has your request.")).to_be_visible()


def _assign(page, email, role):
    propose_role(page, email, role)


def test_owner_requests_tab(page, seed, login, axe, tmp_path):
    m2.residents_enabled(True)
    login(OWNER_EMAIL)
    page.goto("/me/requests/")
    chooser = page.get_by_test_id("req-chooser")
    expect(chooser.get_by_test_id("req-new-maint")).to_be_visible()
    expect(chooser.get_by_test_id("req-new-arc")).to_be_visible()
    expect(page.get_by_text("You have no requests yet.")).to_be_visible()
    axe(page)

    chooser.get_by_test_id("req-new-arc").click()
    expect(page).to_have_url(re.compile(r"/me/arc/new/$"))
    page.goto("/me/requests/")
    page.get_by_test_id("req-chooser").get_by_test_id("req-new-maint").click()
    expect(page).to_have_url(re.compile(r"/me/maintenance/new/$"))

    _owner_submits(page, tmp_path)
    lst = page.get_by_test_id("req-list")

    _backdate(arc=(2026, 9, 1), mnt=(2026, 9, 20))
    page.goto("/me/requests/")
    assert _titles(page) == [MNT_T, ARC_T], _titles(page)
    expect(lst.locator('[data-kind="arc"]')).to_contain_text("Submitted")
    expect(lst.locator('[data-kind="arc"]')).to_contain_text("Architectural")
    expect(lst.locator('[data-kind="maintenance"]')).to_contain_text("Maintenance")
    expect(lst.locator('[data-kind="maintenance"]')).to_contain_text("New")
    axe(page)

    _backdate(arc=(2026, 9, 1), mnt=(2026, 8, 1))
    page.reload()
    assert _titles(page) == [ARC_T, MNT_T], _titles(page)
    lst.locator('[data-kind="arc"]').click()
    expect(page).to_have_url(re.compile(r"/me/arc/\d+/$"))
    page.goto("/me/requests/")
    lst.locator('[data-kind="maintenance"]').click()
    expect(page).to_have_url(re.compile(r"/me/maintenance/\d+/$"))


def test_tenant_sees_maintenance_only(page, browser, base_url, seed, login, axe, tmp_path):
    m2.residents_enabled(True)
    # Board logs an ARC request on unit E: the tenant must not see it.
    login(BOARD_EMAIL)
    page.goto("/requests/architectural/new/")
    page.locator("#arc-unit").select_option(label="Unit E")
    page.locator("#arc-form").select_option(label="Exterior change")
    page.get_by_role("button", name="Continue").click()
    page.get_by_label("Short title").fill("Owner E new deck")
    page.get_by_label("Describe the change").fill("Deck")
    page.get_by_label("Where on the property").fill("Back")
    page.get_by_label("Type of change").select_option(label="Windows or doors")
    page.get_by_role("button", name="Submit request").click()
    expect(page.get_by_text("Request submitted for Unit E.")).to_be_visible()

    ctx = browser.new_context(base_url=base_url)
    t = ctx.new_page()
    login(TENANT_EMAIL, t)
    t.goto("/me/maintenance/new/")
    t.get_by_label("Where is it?").fill("Back gutter")
    t.get_by_label("What needs fixing?").fill("Tenant gutter drip")
    t.get_by_label("Details").fill("Drips")
    t.get_by_label("Photos").set_input_files(m5.png(tmp_path, "g.png"))
    t.get_by_test_id("me-maint-submit").click()
    expect(t.get_by_text("Thanks. The board has your request.")).to_be_visible()
    t.goto("/me/requests/")
    chooser = t.get_by_test_id("req-chooser")
    expect(chooser.get_by_test_id("req-new-maint")).to_be_visible()
    expect(chooser.get_by_test_id("req-new-arc")).to_have_count(0)
    lst = t.get_by_test_id("req-list")
    expect(lst.locator('[data-kind="maintenance"]')).to_have_count(1)
    expect(lst).to_contain_text("Tenant gutter drip")
    expect(lst.locator('[data-kind="arc"]')).to_have_count(0)
    expect(lst).not_to_contain_text("Owner E new deck")
    axe(t)
    ctx.close()


def test_preview_requests_read_only(page, browser, base_url, seed, login, axe, tmp_path):
    m2.residents_enabled(True)
    ctx = browser.new_context(base_url=base_url)
    o = ctx.new_page()
    login(OWNER_EMAIL, o)
    _owner_submits(o, tmp_path)
    ctx.close()
    m2.residents_enabled(False)  # Preview works while residents are off

    login(BOARD_EMAIL)
    page.goto("/units/G/preview/requests/")
    expect(page.get_by_test_id("preview-banner")).to_be_visible()
    chooser = page.get_by_test_id("req-chooser")
    expect(chooser.locator("a")).to_have_count(0)
    expect(chooser.locator('[aria-disabled="true"]')).to_have_count(2)
    lst = page.get_by_test_id("req-list")
    expect(lst.locator("a")).to_have_count(0)
    expect(lst).to_contain_text(ARC_T)
    expect(lst).to_contain_text(MNT_T)
    axe(page)

    page.goto("/units/G/preview/requests/?as=tenant")
    expect(page.get_by_test_id("req-chooser").locator('[aria-disabled="true"]')).to_have_count(1)
    expect(lst).to_contain_text(MNT_T)
    expect(lst).not_to_contain_text(ARC_T)


def test_unit_file_sections_by_role(page, browser, base_url, seed, login, axe, tmp_path):
    login(BOARD_EMAIL)
    _category(page)
    _log(page, tmp_path, unit="G")
    # ARC and maintenance on unit G from the owner.
    m2.residents_enabled(True)
    ctx = browser.new_context(base_url=base_url)
    o = ctx.new_page()
    login(OWNER_EMAIL, o)
    _owner_submits(o, tmp_path)
    ctx.close()

    page.goto("/units/G/file/")
    rows = page.get_by_test_id("unit-file").locator("tbody tr[data-kind]")
    expect(rows.first).to_be_visible()
    kinds = set(rows.evaluate_all("rs => rs.map(r => r.dataset.kind)"))
    assert {"violations", "arc", "maintenance"} <= kinds, kinds
    file = page.get_by_test_id("unit-file")
    expect(file.locator('tr[data-kind="arc"]')).to_contain_text(ARC_T)
    expect(file.locator('tr[data-kind="maintenance"]')).to_contain_text(MNT_T)
    expect(file.locator('tr[data-kind="violations"]')).to_have_count(1)
    axe(page)

    _assign(page, COMMITTEE_EMAIL, "Committee")
    _assign(page, CPA_EMAIL, "CPA (read-only)")
    for email in (COMMITTEE_EMAIL, CPA_EMAIL):
        c = browser.new_context(base_url=base_url)
        p = c.new_page()
        login(email, p)
        resp = p.goto("/units/G/file/")
        assert resp and resp.status == 403, f"{email} got {resp and resp.status}"
        c.close()
