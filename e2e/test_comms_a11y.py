"""Axe scan of the per-unit comm log."""

from playwright.sync_api import expect

from conftest import BOARD_EMAIL

UNIT = "G"  # seeded by seed_demo


def test_comm_log_axe(page, seed, login, axe):
    login(BOARD_EMAIL)
    resp = page.goto(f"/units/{UNIT}/comms/")
    assert resp and resp.status == 200, f"comm log returned {resp and resp.status}"
    expect(page.get_by_test_id("comm-log")).to_be_visible()
    axe(page)
