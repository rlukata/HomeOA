"""Maintenance requests + work orders linked to AP bills (MNT-01, MNT-02).
Tenant submits a gutter leak with a photo -> work order to Evergreen ->
status updates -> AP bill linked by hand; cost = bill amount; bill detail links back.

Board: /requests/maintenance/ (list, add/, <pk>/, <pk>/work-order/) and
/requests/work-orders/<pk>/. Resident: /me/maintenance/ and new/. "Create bill from
work order" prefills the vendor only. Tenant chris.doyle = unit E."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, SECRETARY_EMAIL, TENANT_EMAIL, manage
from helpers import m2, m5

M = "/requests/maintenance/"
VENDOR = "Evergreen Gutter & Roof"
LEAK = "Gutter leaking onto the patio"

_APPROVE = """
from django.contrib.auth import get_user_model
from homeoa.ap.approvals import approve
from homeoa.ap.models import Bill
from homeoa.core.context import association_context
b = Bill.all_associations.get(pk=PK)
with association_context(b.association):
    approve(b, get_user_model().objects.get(email=WHO))
print("OK")
"""


def _tenant_submits(browser, base_url, login, tmp_path):
    ctx = browser.new_context(base_url=base_url)
    t = ctx.new_page()
    login(TENANT_EMAIL, t)
    t.goto("/me/maintenance/new/")
    t.get_by_label("Where is it?").fill("Back gutter over patio")
    t.get_by_label("What needs fixing?").fill(LEAK)
    t.get_by_label("Details").fill("Leaks when it rains")
    t.get_by_label("Photos").set_input_files(m5.png(tmp_path, "gutter.png"))
    t.get_by_test_id("me-maint-submit").click()
    expect(t.get_by_text("Thanks. The board has your request.")).to_be_visible()
    t.goto("/me/maintenance/")
    expect(t.get_by_test_id("me-maint-list")).to_contain_text(LEAK)
    ctx.close()


def _board_request(page, unit_label, summary, location="Somewhere"):
    page.goto(M + "add/")
    page.locator("select[name=unit]").select_option(label=unit_label)
    page.get_by_label("Location").fill(location)
    page.get_by_label("Summary").fill(summary)
    page.get_by_test_id("maint-save").click()
    expect(page.get_by_text("Maintenance request saved.")).to_be_visible()


def _new_wo(page, scope="Reseal back gutter seams", estimate="400.00") -> str:
    page.get_by_test_id("wo-create").click()
    page.locator("select[name=vendor]").select_option(label=VENDOR)
    page.get_by_label("Scope of work").fill(scope)
    page.get_by_label("Estimated cost").fill(estimate)
    page.get_by_test_id("wo-save").click()
    expect(page.get_by_text("Work order created.")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/requests/work-orders/\d+/$"))
    return page.url


def test_maintenance_to_bill(page, browser, base_url, seed, login, axe, tmp_path):
    m2.residents_enabled(True)
    _tenant_submits(browser, base_url, login, tmp_path)

    login(BOARD_EMAIL)
    page.goto("/")
    expect(page.get_by_text(f"Triage maintenance request: {LEAK}")).to_be_visible()
    page.goto(M)
    page.get_by_role("link", name=LEAK).click()
    expect(page.get_by_test_id("req-status")).to_have_text("New")
    expect(page.get_by_test_id("maint-photo").locator("img")).to_have_count(1)
    axe(page)

    wo_url = _new_wo(page)
    expect(page.get_by_test_id("wo-status")).to_have_text("Draft")
    for status in ("Sent", "Scheduled", "In progress", "Completed"):
        page.locator("select[name=status]").select_option(label=status)
        page.get_by_test_id("wo-status-save").click()
        expect(page.get_by_test_id("wo-status")).to_have_text(status)
    expect(page.get_by_test_id("status-history").locator("li")).to_have_count(5)
    axe(page)

    # Create bill from work order: vendor prefilled, then the normal bill flow.
    page.get_by_test_id("wo-create-bill").click()
    vendor = page.locator("select[name=vendor]")
    expect(vendor.locator("option:checked")).to_have_text(VENDOR)
    page.get_by_label("Invoice number").fill("EGR-5501")
    page.get_by_label("Bill date").fill("2026-10-06")
    page.get_by_label("Due date").fill("2026-11-05")
    page.locator("[name$='-0-account']").select_option(label="5200 · Repairs & maintenance")
    page.locator("[name$='-0-fund']").select_option(label="Operating")
    page.locator("[name$='-0-amount']").fill("385.50")
    page.get_by_test_id("bill-save").click()
    expect(page.get_by_text("Draft saved.")).to_be_visible()
    page.get_by_test_id("bill-submit").click()
    expect(page.get_by_test_id("bill-status")).to_have_text("Pending approval")
    pk = int(re.search(r"/money/bills/(\d+)/", page.url).group(1))
    r = manage("shell", "-c", f"PK={pk}\nWHO={SECRETARY_EMAIL!r}\n" + _APPROVE, check=False)
    assert r.stdout.strip().endswith("OK"), r.stderr[-1500:]

    # A bill created from the work order is linked on save.
    page.goto(wo_url)
    expect(page.get_by_test_id(f"wo-bill-{pk}")).to_contain_text("EGR-5501")
    expect(page.get_by_test_id("wo-actual")).to_contain_text("$385.50")

    page.goto(f"/money/bills/{pk}/")
    page.get_by_test_id("bill-work-order").click()
    expect(page).to_have_url(re.compile(re.escape(wo_url) + "$"))


def test_board_enters_on_behalf_common_area(page, seed, login, axe):
    login(BOARD_EMAIL)
    _board_request(page, "Common area", "Garage door opener grinding", "Garage door")
    expect(page.get_by_test_id("req-status")).to_have_text("New")
    axe(page)
    page.goto(M)
    expect(page.get_by_role("link", name="Garage door opener grinding")).to_be_visible()


def test_link_bill_limited_to_vendor(page, seed, login):
    """The Link bill picker lists only the work order vendor's bills."""
    login(BOARD_EMAIL)
    _board_request(page, "Unit I", "Deck drain clogged", "Unit I deck")
    _new_wo(page, scope="Clear drain", estimate="150.00")
    picker = page.locator("select[name=bill]")
    if picker.count():
        opts = picker.locator("option").all_inner_texts()
        assert not any("Greenway" in o for o in opts), opts
    # A forged POST with another vendor's bill is refused.
    wo = int(re.search(r"/work-orders/(\d+)/", page.url).group(1))
    r = manage(
        "shell",
        "-c",
        "from homeoa.ap.models import Bill\n"
        "b = Bill.all_associations.exclude(vendor__name__startswith='Evergreen').first()\n"
        "print('BILL', b.pk if b else '')",
        check=False,
    )
    other = r.stdout.split("BILL")[-1].strip()
    if other:
        token = page.locator("input[name=csrfmiddlewaretoken]").first.get_attribute("value")
        page.request.post(
            f"/requests/work-orders/{wo}/bills/",
            form={"bill": other, "csrfmiddlewaretoken": token},
            headers={"Referer": page.url},
        )
        page.reload()
        expect(page.get_by_test_id(f"wo-bill-{other}")).to_have_count(0)


def test_tenant_sees_only_own_requests(page, browser, base_url, seed, login, tmp_path):
    login(BOARD_EMAIL)
    _board_request(page, "Unit A", "Board-entered unit A faucet drip", "Unit A kitchen")
    m2.residents_enabled(True)
    _tenant_submits(browser, base_url, login, tmp_path)
    ctx = browser.new_context(base_url=base_url)
    t = ctx.new_page()
    login(TENANT_EMAIL, t)
    t.goto("/me/maintenance/")
    lst = t.get_by_test_id("me-maint-list")
    expect(lst).to_contain_text(LEAK)
    expect(lst).not_to_contain_text("faucet drip")
    ctx.close()


def _bill_basics(page, invoice):
    page.get_by_label("Invoice number").fill(invoice)
    page.get_by_label("Bill date").fill("2026-10-06")
    page.get_by_label("Due date").fill("2026-11-05")
    page.locator("[name$='-0-account']").select_option(label="5200 · Repairs & maintenance")
    page.locator("[name$='-0-fund']").select_option(label="Operating")


def test_create_bill_prefills_and_links(page, seed, login, axe):
    login(BOARD_EMAIL)
    _board_request(page, "Unit I", "Deck drain clogged", "Unit I deck")
    wo_url = _new_wo(page, scope="Clear deck drain", estimate="150.00")
    page.get_by_test_id("wo-create-bill").click()
    expect(page.get_by_test_id("bill-from-work-order")).to_be_visible()
    expect(page.locator("select[name=vendor] option:checked")).to_have_text(VENDOR)
    expect(page.get_by_label("Description")).not_to_have_value("")
    expect(page.locator("[name$='-0-amount']")).to_have_value(re.compile(r"^150(\.00)?$"))
    axe(page)
    _bill_basics(page, "EGR-7001")
    page.get_by_test_id("bill-save").click()
    expect(page.get_by_text("Draft saved.")).to_be_visible()
    pk = int(re.search(r"/money/bills/(\d+)/", page.url).group(1))
    page.goto(wo_url)
    expect(page.get_by_test_id(f"wo-bill-{pk}")).to_contain_text("EGR-7001")


def test_bill_without_work_order(page, seed, login):
    login(BOARD_EMAIL)
    page.goto("/money/bills/add/")
    expect(page.get_by_test_id("bill-from-work-order")).to_have_count(0)
    page.locator("select[name=vendor]").select_option(label=VENDOR)
    _bill_basics(page, "EGR-7002")
    page.locator("[name$='-0-amount']").fill("75.00")
    page.get_by_test_id("bill-save").click()
    expect(page.get_by_text("Draft saved.")).to_be_visible()
    expect(page.get_by_test_id("bill-work-order")).to_have_count(0)
