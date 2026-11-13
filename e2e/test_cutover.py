"""Cutover readiness (FIN-18) at Money > Reports > Cutover: read-only, finance.read.
Ready only when an import is committed, the books are locked through its date, the latest
parallel run is clean, and every board member / ARC reviewer has a login and an email.
Synthetic data only (e2e/fixtures/appfolio)."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL
from helpers.seed import run_script
from test_imports import FIXTURES, import_base, upload_snapshot

CUTOVER = "/money/reports/cutover/"

_PRE = """
import datetime as dt, json
from homeoa.core.context import association_context
from homeoa.core.models import Association
a = Association.objects.order_by("pk").first()
with association_context(a):
"""

# Lock the books through the committed import's as-of date (what closing that month does).
LOCK = (
    _PRE
    + """
    from homeoa.imports.models import ImportRun
    from homeoa.ledger.services import set_locked_through
    r = ImportRun.objects.filter(status=ImportRun.Status.COMMITTED).order_by("as_of").first()
    set_locked_through(a, r.as_of)
    print("E2EJSON" + json.dumps({"as_of": r.as_of.isoformat()}))
"""
)

# The bare seed has board logins but no board terms: give the treasurer a current term.
TERM = (
    _PRE
    + """
    from django.contrib.auth import get_user_model
    from homeoa.governance.models import BoardTerm
    from homeoa.registry.models import Person
    u = get_user_model().objects.get(email=__EMAIL__)
    p = Person.objects.create(association=a, full_name="Tess Treasurer", email=u.email, user=u)
    BoardTerm.objects.create(association=a, person=p, office="treasurer",
        start=dt.date(2026, 1, 1))
    print("E2EJSON" + json.dumps({"person": p.pk}))
"""
)


def _check(page, key):
    return page.locator(f"#check-{key}")


def _status(page):
    return page.get_by_role("region", name="Checks")


def _diff(page, folder, as_of=None):
    page.goto("/money/reports/parallel-run/")
    if as_of:
        page.get_by_label("Balances as of").fill(as_of)
    upload_snapshot(page, folder)
    page.get_by_role("button", name=re.compile("run diff|compare", re.IGNORECASE)).click()
    expect(page).to_have_url(re.compile(r"/parallel-run/\d+/$"))


def test_demo_not_ready_lists_missing(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/money/reports/parallel-run/")
    page.get_by_role("navigation", name="Reports").get_by_role("link", name="Cutover").click()
    expect(page).to_have_url(re.compile(CUTOVER + "$"))
    expect(page.get_by_role("heading", name="Cutover readiness", level=1)).to_be_visible()
    expect(_status(page).get_by_text("Not ready", exact=True)).to_be_visible()
    expect(_status(page).get_by_text("Ready", exact=True)).to_have_count(0)
    expect(_check(page, "import")).to_contain_text("No import has been committed yet.")
    expect(_check(page, "import")).to_contain_text("To do")
    expect(_check(page, "lock")).to_contain_text("To do")
    expect(_check(page, "parallel")).to_contain_text("No parallel-run comparison yet.")
    people = _check(page, "people")
    expect(people).to_contain_text("To do")
    expect(people.locator("ul li")).to_have_text(
        ["Rafael Ortiz: no login", "Sofia Herrera: no login, no email"]
    )
    expect(_check(page, "months")).to_contain_text("0 of 2 clean months.")
    expect(_check(page, "records")).to_contain_text("Not yet recorded.")
    glance = page.get_by_role("region", name="At a glance")
    expect(glance).to_contain_text(re.compile(r"\d+ open to-dos?"))
    expect(glance).to_contain_text(re.compile(r"Owners: \d+ · invited \d+ · accepted \d+"))
    expect(glance).to_contain_text(re.compile(r"\d+ unverified rule-pack items?"))
    # Reports nav: the Cutover chip is current; Parallel run still links.
    nav = page.get_by_role("navigation", name="Reports")
    expect(nav.get_by_role("link", name="Cutover")).to_have_attribute("aria-current", "page")
    nav.get_by_role("link", name="Parallel run").click()
    expect(page).to_have_url(re.compile(r"/money/reports/parallel-run/$"))
    page.go_back()
    axe(page)


def test_owner_refused(page, seed, login):
    login(OWNER_EMAIL)
    resp = page.goto(CUTOVER)
    assert resp.status == 403, resp.status
    expect(page.get_by_role("heading", name="Cutover readiness")).to_have_count(0)


def test_anonymous_redirected(page, seed):
    page.goto(CUTOVER)
    expect(page).to_have_url(re.compile("login"))


def test_path_to_ready(page, seed_bare, login, axe):
    login(BOARD_EMAIL)
    page.goto(CUTOVER)
    expect(_check(page, "people")).to_contain_text("No current board terms are recorded.")

    import_base(page)
    page.goto(CUTOVER)
    expect(_check(page, "import")).to_contain_text("Done")
    expect(_check(page, "lock")).to_contain_text("The books are not locked yet.")

    # A later snapshot with a late fee differs: latest run is not clean.
    _diff(page, FIXTURES / "later_unit7_latefee")
    page.goto(CUTOVER)
    expect(_check(page, "parallel")).to_contain_text("unexplained differences")
    expect(_check(page, "parallel")).to_contain_text("To do")

    as_of = run_script(LOCK, "lock")["as_of"]
    run_script(TERM.replace("__EMAIL__", repr(BOARD_EMAIL)), "board term")
    page.goto(CUTOVER)
    expect(_check(page, "lock")).to_contain_text(f"Books locked through {as_of}.")
    expect(_check(page, "lock")).to_contain_text("Done")
    expect(_check(page, "people")).to_contain_text("Done")
    expect(_status(page).get_by_text("Not ready", exact=True)).to_be_visible()

    # The imported snapshot compares clean against itself, but 1 of 2 clean months is not enough.
    _diff(page, FIXTURES / "base")
    page.goto(CUTOVER)
    expect(_check(page, "parallel")).to_contain_text("matches to the cent")
    expect(_check(page, "months")).to_contain_text("1 of 2 clean months.")
    expect(_check(page, "months")).to_contain_text("To do")
    months = page.get_by_role("table", name="Diff status per month")
    expect(months.get_by_role("row", name=re.compile("October 2026"))).to_contain_text("Clean")
    expect(_status(page).get_by_text("Not ready", exact=True)).to_be_visible()

    # A second clean month (Nov 5; the month table stops at today, Oct 2026).
    _diff(page, FIXTURES / "base", as_of="2026-11-05")
    page.goto(CUTOVER)
    expect(_check(page, "months")).to_contain_text("2 of 2 clean months.")
    expect(_check(page, "months")).to_contain_text("Done")
    expect(_check(page, "records")).to_contain_text("Not yet recorded.")
    expect(_status(page).get_by_text("Not ready", exact=True)).to_be_visible()

    _request_records(page)
    expect(_check(page, "records")).to_contain_text("Done")
    expect(_status(page).get_by_text("Ready", exact=True)).to_be_visible()
    expect(page.get_by_text("Clean months so far: 2.")).to_be_visible()
    axe(page)
    page.get_by_role("link", name="Open the report").click()
    expect(page).to_have_url(re.compile(r"/money/reports/parallel-run/\d+/$"))


def _request_records(page):
    page.get_by_label("Records delivery requested").check()
    page.get_by_role("region", name="Records from the management company").get_by_role(
        "button", name="Save"
    ).click()
    expect(page.get_by_text("Records delivery request saved.")).to_be_visible()


def test_records_checkbox_audited(page, seed, login):
    login(BOARD_EMAIL)
    page.goto(CUTOVER)
    _request_records(page)
    expect(page.get_by_text("Marked requested on 2026-10-07 by " + BOARD_EMAIL)).to_be_visible()
    expect(page.get_by_label("Records delivery requested")).to_be_checked()
    page.goto("/settings/audit/")
    expect(page.get_by_text("Cutover: management company records delivery").first).to_be_visible()


def test_clean_months_setting_requires_manage(page, seed, login):
    login(BOARD_EMAIL)
    page.goto(CUTOVER)
    page.get_by_label("Clean months required before cutover").fill("3")
    page.get_by_role("region", name="Parallel run by month").get_by_role(
        "button", name="Save"
    ).click()
    expect(page.get_by_text("Clean months required saved.")).to_be_visible()
    expect(_check(page, "months")).to_contain_text("0 of 3 clean months.")

    # Secretary: board, finance.read without finance.manage -> read-only.
    login(SECRETARY_EMAIL)
    resp = page.goto(CUTOVER)
    assert resp.status == 200, resp.status
    expect(_check(page, "months")).to_contain_text("0 of 3 clean months.")
    expect(page.locator("main form")).to_have_count(0)
    expect(page.get_by_label("Records delivery requested")).to_have_count(0)
    token = next(c["value"] for c in page.context.cookies() if c["name"] == "csrftoken")
    resp = page.request.post(
        CUTOVER,
        form={"action": "records", "requested": "1"},
        headers={"X-CSRFToken": token, "Referer": page.url},
    )
    assert resp.status == 403, resp.status
    page.reload()
    expect(_check(page, "records")).to_contain_text("Not yet recorded.")
