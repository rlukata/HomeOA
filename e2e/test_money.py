"""Dues, payments, bank import/match, SimpleFIN, reconcile, late fees."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from conftest import BOARD_EMAIL, manage
from helpers.flows import post_dues, record_payment

FIXTURES = Path(__file__).parent / "fixtures"


def balance(page, unit):
    page.goto(f"/units/{unit}/")
    return page.get_by_test_id("unit-balance")


def test_post_monthly_dues(page, seed, login):
    # Seed has Aug-Sep history and leaves October unposted.
    login(BOARD_EMAIL)
    post_dues(page, "2026-10")
    expect(page.get_by_text(re.compile(r"9 charges"))).to_be_visible()
    expect(page.get_by_text("$3,432.36")).to_be_visible()
    expect(page.get_by_text("$396.63").first).to_be_visible()
    expect(page.get_by_text("$350.86").first).to_be_visible()
    # TODO: exact per-unit dues for the 9-unit A-I seed.

    page.goto("/money/reports/trial-balance/")
    expect(page.get_by_test_id("tb-difference")).to_have_text("$0.00")
    # Operating vs reserve split per configured schedule (TODO: exact split values).
    expect(page.get_by_role("row", name=re.compile("Reserve", re.IGNORECASE)).first).to_be_visible()

    # Idempotent re-run.
    post_dues(page, "2026-10")
    expect(page.get_by_text(re.compile("already posted|0 charges", re.IGNORECASE))).to_be_visible()


def test_record_check_payment(page, seed, login):
    login(BOARD_EMAIL)
    before = balance(page, "G").inner_text()
    alloc = record_payment(page, "G", "350.86", "1042")
    # Applied to the oldest open charge (generic pack allocation order).
    expect(alloc).to_contain_text(re.compile("Sep", re.IGNORECASE))  # seed: unit G owes Sep
    assert balance(page, "G").inner_text() != before

    page.goto("/settings/audit/")
    row = page.get_by_role("row", name=re.compile("payment", re.IGNORECASE)).first
    expect(row).to_contain_text(BOARD_EMAIL.split("@")[0], ignore_case=True)


@pytest.mark.parametrize("statement", ["bank_oct.csv", "bank_oct.ofx"])
def test_bank_import_and_match(page, seed, login, statement):
    login(BOARD_EMAIL)
    post_dues(page, "2026-10")  # so the Ortiz Zelle has an open October charge to match
    page.goto("/money/bank/import/")
    page.get_by_label(re.compile("file", re.IGNORECASE)).set_input_files(FIXTURES / statement)
    page.get_by_role("button", name=re.compile("upload|import", re.IGNORECASE)).click()

    page.goto("/")
    item = page.get_by_role("listitem").filter(has_text="Match deposit: Zelle from RAFAEL ORTIZ")
    expect(item).to_contain_text("$396.63")
    expect(item).to_contain_text("Unit I")
    item.get_by_role("button", name="Confirm").click()
    expect(item).to_have_count(0)
    expect(balance(page, "I")).to_have_text("$0.00")

    page.goto("/")
    cat = page.get_by_role("listitem").filter(has_text="KING COUNTY TREAS")
    expect(cat).to_contain_text("$212.18")
    cat.get_by_role("link", name="Categorize").click()
    # Home opens the transaction page; its Categorize link leads to the form.
    page.get_by_role("link", name="Categorize", exact=True).last.click()
    acct = page.get_by_label(re.compile("account", re.IGNORECASE))
    opt = acct.locator("option", has_text="Other income").first.get_attribute("value")
    acct.select_option(opt)
    page.get_by_role("button", name=re.compile("save|categorize", re.IGNORECASE)).click()
    expect(page.get_by_text("Categorized as Other income.")).to_be_visible()


def test_simplefin_sync_fake(page, seed, login):
    login(BOARD_EMAIL)
    # seed_demo connects the SimpleFIN feed (fake bridge); the plugin row shows it enabled.
    page.goto("/settings/plugins/")
    expect(page.get_by_role("row", name=re.compile("SimpleFIN", re.IGNORECASE))).to_be_visible()
    page.goto("/money/bank/")
    page.get_by_role("button", name="Sync now").click()
    txns = page.get_by_test_id("bank-txn")
    expect(txns.first).to_be_visible()
    n = txns.count()
    page.get_by_role("button", name="Sync now").click()
    expect(txns).to_have_count(n)
    # The fake bridge's "now" is Oct 7 2026, 9:12 am Pacific (fake_server.SYNCED_AT).
    expect(page.get_by_text(re.compile(r"synced 9:12 am", re.IGNORECASE))).to_be_visible()


def test_simplefin_synced_today_under_fixed_clock(page, seed, login):
    login(BOARD_EMAIL)
    page.goto("/money/bank/")
    page.get_by_role("button", name="Sync now").click()
    expect(page.get_by_text(re.compile(r"synced 9:12 am(?! on )", re.IGNORECASE))).to_be_visible(
        timeout=2000
    )


def test_reconcile_month(page, seed, login):
    login(BOARD_EMAIL)
    # Seed leaves the Sep 30 interest unmatched; categorize it so September has a matched txn.
    page.goto("/money/bank/")
    page.get_by_role("link", name="INTEREST PAYMENT").first.click()
    page.get_by_role("link", name="Categorize", exact=True).last.click()
    page.get_by_role("button", name="Categorize").click()
    expect(page.get_by_text(re.compile("Categorized as"))).to_be_visible()
    page.goto("/money/bank/reconcile/")
    # Seed: Reserve savings is $41,950.00 after the Sep 30 interest, with no later activity.
    acct = page.get_by_label("Bank account")
    acct.select_option(acct.locator("option", has_text="Reserve").first.get_attribute("value"))
    page.get_by_label(re.compile("statement date", re.IGNORECASE)).fill("2026-09-30")
    page.get_by_label(re.compile("ending balance", re.IGNORECASE)).fill("41950.00")
    page.get_by_label(re.compile("beginning balance", re.IGNORECASE)).fill("41915.04")
    page.get_by_role("button", name=re.compile("start|continue", re.IGNORECASE)).click()
    expect(page.get_by_test_id("recon-difference")).to_have_text("$0.00")
    page.get_by_role(
        "button", name=re.compile("finish|complete|mark reconciled", re.IGNORECASE)
    ).click()
    expect(page.get_by_text(re.compile("reconciled", re.IGNORECASE)).first).to_be_visible()

    page.goto("/money/bank/")
    page.locator('[data-testid="bank-txn"]:not([data-status="unmatched"])').filter(
        has_text="Sep"
    ).first.get_by_role("link").first.click()
    expect(page.get_by_role("button", name=re.compile("edit|undo", re.IGNORECASE))).to_be_disabled()


def test_late_fee_auto(page, seed, login):
    # seed_demo carries the placeholder late-fee policy: $25 flat after a 10-day grace from the
    # Sep 10 due date, so the Sep 21 run charges unit G.
    login(BOARD_EMAIL)
    # Scheduler run for a fixed date: unit G has not paid September by the 21st.
    manage("run_scheduled", "--as-of", "2026-09-21", "--rerun")
    page.goto("/money/assessments/late-fees/")

    def unit_rows(n):
        cell = page.get_by_role("cell", name=n, exact=True)
        return page.get_by_role("table").last.get_by_role("row").filter(has=cell)  # fees table

    # Before the first notice the fee is capped by the rule pack (unverified WA value),
    # so accept either the full $25.00 or a capped amount marked "Capped".
    expect(unit_rows("G").first).to_contain_text(re.compile(r"\$25\.00|Capped"))
    expect(unit_rows("A")).to_have_count(0)


def _import_csv(page, path):
    page.goto("/money/bank/import/")
    page.get_by_label(re.compile("file", re.IGNORECASE)).set_input_files(path)
    page.get_by_role("button", name=re.compile("upload|import", re.IGNORECASE)).click()


def test_payee_categorized_once_is_suggested_next_time(page, seed, login, tmp_path):
    login(BOARD_EMAIL)
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    first.write_text(
        "Date,Description,Amount\n2026-10-02,PUGET SOUND ENERGY 1002 REF 4411,-180.25\n"
    )
    second.write_text(
        "Date,Description,Amount\n2026-10-06,PUGET SOUND ENERGY 1006 REF 9873,-192.10\n"
    )
    _import_csv(page, first)
    page.goto("/money/bank/")
    page.get_by_role("link", name=re.compile("PUGET SOUND ENERGY 1002")).click()
    page.get_by_role("link", name="Categorize", exact=True).last.click()
    acct = page.get_by_label(re.compile("account", re.IGNORECASE))
    acct.select_option(acct.locator("option", has_text="Utilities").first.get_attribute("value"))
    page.get_by_role("button", name=re.compile("save|categorize", re.IGNORECASE)).click()
    expect(page.get_by_text("Categorized as Utilities.")).to_be_visible()

    _import_csv(page, second)
    page.goto("/money/bank/")
    page.get_by_role("link", name=re.compile("PUGET SOUND ENERGY 1006")).click()
    suggestion = page.get_by_test_id("suggestion").first
    expect(suggestion).to_contain_text("Categorize as Utilities")
    expect(suggestion).to_contain_text("same payee, categorized this way on Oct 2, 2026")
    # The Categorize form starts on the learned account.
    page.get_by_role("link", name="Categorize", exact=True).first.click()
    expect(page.get_by_label(re.compile("account", re.IGNORECASE))).to_have_value(
        page.get_by_label(re.compile("account", re.IGNORECASE))
        .locator("option", has_text="Utilities")
        .first.get_attribute("value")
    )


def test_money_overview_says_the_month_in_plain_words(page, seed, login, axe):
    login(BOARD_EMAIL)
    page.goto("/")
    page.get_by_role("link", name="Money", exact=True).first.click()
    story = page.get_by_test_id("money-story")
    expect(story).to_contain_text("October so far")
    expect(story).to_contain_text(re.compile(r"Operating cash (rose|fell|stayed)"))
    expect(story).to_contain_text("Spending this year so far")
    axe(page)
