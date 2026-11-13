"""Owner answers a violation (VIO-06). After the courtesy notice, owner A sees the
violation on Requests (rule, description, notice; never the reporter), sends "I disagree"
with a photo; the board sees the response on the violation and an "offer a hearing"
to-do on Home."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2, m5
from test_escalation import _open


def test_owner_disagrees_board_offers_hearing(page, seed, login, axe, tmp_path):
    m2.residents_enabled(True)
    login(BOARD_EMAIL)
    board_url = _open(page, tmp_path, "A")
    page.get_by_test_id("send-courtesy").click()
    expect(page.get_by_text("Notice sent by email.")).to_be_visible()
    page.context.clear_cookies()

    login(m2.OWNERA_EMAIL)
    page.goto("/me/requests/")
    page.get_by_test_id("violations-list").locator("a").first.click()
    expect(page.get_by_test_id("notices")).to_contain_text("Courtesy notice")
    axe(page)
    form = page.get_by_test_id("respond-form")
    form.get_by_label("I disagree").check()
    form.get_by_label("Message to the board").fill("The bins were out for pickup day.")
    form.get_by_label("Photos (optional)").set_input_files(m5.png(tmp_path, "proof.png"))
    form.get_by_role("button", name="Send to the board").click()
    expect(page.get_by_text("Sent to the board.")).to_be_visible()
    expect(page.get_by_test_id("responses")).to_contain_text("The bins were out")
    page.context.clear_cookies()

    login(BOARD_EMAIL)
    page.goto("/")
    expect(page.get_by_text("Owner disagrees: offer a hearing")).to_be_visible()
    page.goto(board_url)
    responses = page.get_by_test_id("responses")
    expect(responses).to_contain_text("I disagree")
    expect(responses.get_by_role("link", name=re.compile("Photo 1"))).to_be_visible()
