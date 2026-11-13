"""Year-end CPA package (FIN-16), read-only CPA role, Vendors > 1099 tab (FIN-19)."""

import csv
import io
import re
import zipfile

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.flows import propose_role
from helpers.seed import run_script

CPA_EMAIL = "cpa@ledgerworks.example"
FY = "2026"  # demo fiscal year starts in January; today is 2026-10-07
STEMS = (
    "trial-balance-accrual",
    "trial-balance-cash",
    "general-ledger",
    "bank-reconciliations",
    "receivables-aging",
    "payables-aging",
    "income-statement",
    "balance-sheet",
    "1099-vendors",
    "form-1120-h-worksheet",
)
PKG = "/money/reports/cpa-package/"
TEN99 = "/vendors/1099/"

# Only two 1099-relevant vendors: Rainier (eligible, no W-9, $412.50 + $87.50 paid in 2026)
# and Puget Supply (not eligible, $99.00 paid in 2026). Seed vendors are made ineligible.
VENDORS = """
import datetime as dt, json
from decimal import Decimal
from homeoa.access.models import RoleAssignment
from homeoa.ap import services as ap
from homeoa.ap.models import Bill, BillLine
from homeoa.banking.models import BankAccount
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.models import Account, Fund
from homeoa.vendors import services as vs
from homeoa.vendors.models import Vendor
from django.contrib.auth import get_user_model
a = Association.objects.order_by("pk").first()
user = get_user_model().objects.get(email=__BOARD__)
with association_context(a):
    Vendor.objects.update(is_1099_eligible=False)
    op = Fund.objects.get(code="operating")
    bank = BankAccount.objects.filter(fund=op).order_by("pk").first()
    def paid(vendor, n, amount, billed, on):
        b = Bill.objects.create(vendor=vendor, invoice_number=f"E2E-{n}", bill_date=billed)
        BillLine.objects.create(bill=b, account=Account.objects.get(code="5200"), fund=op,
                                amount=Decimal(amount))
        b = ap.submit(b, user)
        if b.status != Bill.Status.APPROVED:
            Bill.objects.filter(pk=b.pk).update(status=Bill.Status.APPROVED)
            b.refresh_from_db()
        ap.mark_paid(b, user, paid_on=on, bank_accounts={op: bank})
    r = vs.save_vendor(Vendor(name="Rainier Plumbing", slug="rainier-plumbing",
                              is_1099_eligible=True, tin="123456789",
                              legal_name="Rainier Plumbing LLC"))
    p = vs.save_vendor(Vendor(name="Puget Supply", slug="puget-supply", is_1099_eligible=False))
    paid(r, 1, "412.50", dt.date(2026, 3, 2), dt.date(2026, 3, 20))
    paid(r, 2, "87.50", dt.date(2026, 8, 3), dt.date(2026, 9, 1))
    paid(p, 3, "99.00", dt.date(2026, 4, 1), dt.date(2026, 4, 10))
    print("E2EJSON" + json.dumps({"rainier": r.slug}))
""".replace("__BOARD__", repr(BOARD_EMAIL))


@pytest.fixture
def books(seed):
    return run_script(VENDORS, "1099 vendors")


def _rows(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


def _download_zip(page) -> zipfile.ZipFile:
    page.goto(PKG)
    page.get_by_test_id("cpa-year").select_option(FY)
    page.get_by_role("button", name="Show").click()
    with page.expect_download() as dl:
        page.get_by_test_id("cpa-download").click()
    d = dl.value
    assert d.suggested_filename.endswith(f"cpa-package-{FY}.zip"), d.suggested_filename
    return zipfile.ZipFile(d.path())


def _money(s: str) -> str:
    return re.sub(r"[^\d.\-]", "", s)


def _invite_cpa(page) -> None:
    """Settings > Roles, as the treasurer (also System admin); the secretary approves."""
    propose_role(page, CPA_EMAIL, "CPA (read-only)")


def test_treasurer_downloads_package_tb_matches_screen(page, books, login, axe):
    login(BOARD_EMAIL)
    page.goto("/money/reports/")
    page.get_by_role("link", name=re.compile("Year-end CPA package")).first.click()
    expect(page).to_have_url(re.compile(re.escape(PKG)))
    axe(page)
    zf = _download_zip(page)
    names = sorted(zf.namelist())
    assert names == sorted(f"{s}-{FY}.csv" for s in STEMS), names

    tb = _rows(zf.read(f"trial-balance-accrual-{FY}.csv"))
    totals = next(r for r in tb if r and r[0] == "Totals")
    nums = [c for c in totals[1:] if c.strip()]
    page.goto(f"/money/reports/trial-balance/?as_of={FY}-12-31&basis=accrual")
    foot = page.locator("tfoot tr").first.locator("td.num")
    screen = [_money(foot.nth(0).inner_text()), _money(foot.nth(1).inner_text())]
    assert [_money(n) for n in nums[-2:]] == screen, (nums, screen)
    assert screen[0] == screen[1]


def test_1099_tab_eligible_only_and_matches_zip(page, books, login, axe):
    login(BOARD_EMAIL)
    page.goto("/vendors/")
    page.get_by_role("link", name="1099 tracking").first.click()
    expect(page).to_have_url(re.compile(re.escape(TEN99)))
    page.get_by_test_id("ten99-year").select_option(FY)
    page.get_by_role("button", name="Show").click()
    rows = page.locator("tr[data-testid^='ten99-row-']")
    expect(rows).to_have_count(1)
    expect(page.get_by_test_id("ten99-row-rainier-plumbing")).to_contain_text("$500.00")
    expect(page.get_by_test_id("ten99-row-puget-supply")).to_have_count(0)
    expect(page.get_by_test_id("ten99-total")).to_have_text("$500.00")
    expect(page.get_by_test_id("ten99-missing-w9")).to_contain_text("Rainier Plumbing")
    row = page.get_by_test_id("ten99-row-rainier-plumbing")
    expect(row).to_contain_text("6789")
    expect(row).not_to_contain_text("123456789")
    axe(page)

    with page.expect_download() as dl:
        page.locator('a[data-export="csv"]').first.click()
    tab = _rows(dl.value.path().read_bytes())
    zf = _download_zip(page)
    assert _rows(zf.read(f"1099-vendors-{FY}.csv")) == tab


def test_cpa_read_only(page, books, login, axe):
    login(BOARD_EMAIL)
    _invite_cpa(page)
    page.context.clear_cookies()
    login(CPA_EMAIL)
    for path in ("/money/reports/", "/money/reports/trial-balance/", "/vendors/", TEN99,
                 "/vendors/rainier-plumbing/", PKG):  # fmt: skip
        resp = page.goto(path)
        assert resp and resp.status == 200, (path, resp and resp.status)
        assert page.locator("main form[method='post' i]").count() == 0, path
    axe(page)
    page.goto("/vendors/")
    expect(page.get_by_role("link", name="Add vendor")).to_have_count(0)
    page.goto("/vendors/rainier-plumbing/")
    tin = page.get_by_test_id("vendor-tin")
    expect(tin).to_contain_text("6789")
    expect(tin).not_to_contain_text("345")
    expect(page.get_by_test_id("reveal-tin")).to_have_count(0)
    expect(page.get_by_role("link", name="Edit")).to_have_count(0)

    resp = page.goto("/money/delinquency/")
    assert resp and resp.status == 403, resp and resp.status

    page.goto("/vendors/")
    token = next((c["value"] for c in page.context.cookies() if c["name"] == "csrftoken"), "")
    headers = {"X-CSRFToken": token, "Referer": page.url}
    bill = page.request.get("/money/bills/").ok  # reading bills is allowed
    assert bill
    posts = (
        ("/money/bills/add/", {}),
        ("/money/bills/1/paid/", {}),
        ("/vendors/add/", {"name": "Sneaky"}),
        ("/vendors/rainier-plumbing/edit/", {"name": "Sneaky"}),
        ("/vendors/rainier-plumbing/tin/", {}),
        ("/money/close/month/", {"month": "2026-09"}),
        ("/money/close/year/", {"year": "2025"}),
    )
    for path, data in posts:
        r = page.request.post(path, form=data, headers=headers)
        assert r.status == 403, (path, r.status)
        assert "CSRF verification failed" not in r.text(), (path, "CSRF refusal")


def test_cpa_downloads_package(page, books, login):
    login(BOARD_EMAIL)
    _invite_cpa(page)
    page.context.clear_cookies()
    login(CPA_EMAIL)
    zf = _download_zip(page)
    assert sorted(zf.namelist()) == sorted(f"{s}-{FY}.csv" for s in STEMS)
