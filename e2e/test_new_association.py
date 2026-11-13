"""A new install: the System admin signs in to an empty database and adds the association
from Home, then lands on Settings."""

import pytest
from playwright.sync_api import expect

from conftest import manage
from helpers.seed import run_script

ADMIN = "admin@newinstall.example"

WIPE = """
import json
from homeoa.core import demo
demo.wipe()
print("E2EJSON" + json.dumps({}))
"""


@pytest.fixture
def empty_install(live_server_url):
    """No association and one System admin. Emptied again afterwards: the next test's
    seed_demo --reset only replaces demo associations."""
    run_script(WIPE, "wipe")
    manage("createsuperuser", "--email", ADMIN, "--noinput")
    yield
    run_script(WIPE, "wipe")


def test_a_new_install_starts_by_adding_the_association(page, empty_install, login, axe):
    login(ADMIN)
    expect(page.get_by_role("heading", name="Add the association")).to_be_visible()
    axe(page)
    form = page.get_by_test_id("new-association-form")
    form.get_by_label("Association name").fill("Maple Court Condominium")
    form.get_by_label("Time zone").select_option("America/Los_Angeles")
    form.get_by_label("Fiscal year starts in").select_option(label="July")
    form.get_by_role("button", name="Add association").click()
    expect(page.get_by_text("Association added.")).to_be_visible()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
    expect(page.get_by_text("Maple Court Condominium").first).to_be_visible()
