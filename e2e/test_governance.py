"""Smoke test: Board & committees page. Add a term -> it shows in
the roster; draft a resolution, record votes, adopt -> number 2026-01, read-only, vote
labels translated; an action item shows on Home for its assignee only."""

import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL, OWNER_EMAIL, SECRETARY_EMAIL
from helpers import m2
from helpers import seed as seed_helpers

PAGE = "/governance/board/"

_PEOPLE = """
import json
from django.contrib.auth import get_user_model
from homeoa.core.models import Association
from homeoa.registry.models import Person
assoc = Association.objects.order_by("pk").first()
out = {}
for email, name in PEOPLE:
    user = get_user_model().objects.get(email__iexact=email)
    p = Person.all_associations.filter(association=assoc, user=user).first()
    if p is None:  # the demo seed links no Person to the board logins
        p = Person.all_associations.create(
            association=assoc, full_name=name, email=email, user=user
        )
    out[email] = str(p)
print("E2EJSON" + json.dumps(out))
"""


def _names():
    people = [(BOARD_EMAIL, "Tessa Treasurer"), (SECRETARY_EMAIL, "Sam Secretary")]
    script = f"PEOPLE={people!r}\n" + _PEOPLE
    names = seed_helpers.run_script(script, "people")
    assert all(names.values()), names
    return names[BOARD_EMAIL], names[SECRETARY_EMAIL]


def _add_term(page, person: str, office: str):
    page.goto(PAGE)
    page.get_by_text("Add a term", exact=True).click()
    form = page.locator("form[action$='/terms/add/']")
    form.get_by_label("Person").select_option(label=person)
    form.get_by_label("Office").select_option(label=office)
    form.get_by_label("Start").fill("2026-01-01")
    form.get_by_label("End").fill("2027-12-31")
    form.get_by_role("button", name="Add term").click()


def test_board_roster_resolution_and_action_item(page, browser, base_url, seed, login, axe):
    treasurer, secretary = _names()
    login(SECRETARY_EMAIL)
    # The Secretary keeps the roster. The demo seed already seats the treasurer and
    # secretary logins; add a new director so the add-term form is still exercised.
    _add_term(page, "Priya Raman", "Director at large")
    page.goto(PAGE)
    roster = page.get_by_test_id("roster")
    expect(roster).to_contain_text(treasurer)
    expect(roster).to_contain_text(secretary)
    expect(roster).to_contain_text("Priya Raman")
    expect(roster).to_contain_text("Director at large")
    axe(page)

    page.locator("form[action$='/resolutions/add/']").get_by_label("Title").fill(
        "Adopt 2027 budget"
    )
    page.get_by_role("button", name="New draft resolution").click()
    page.goto(PAGE)
    page.get_by_test_id("resolutions").get_by_role("link").first.click()
    res_url = page.url
    votes = page.locator("form:has(input[name='action'][value='votes'])")
    votes.get_by_label(treasurer).select_option(label="For")
    votes.get_by_label(secretary).select_option(label="Against")
    page.get_by_role("button", name="Save votes").click()
    page.goto(res_url)
    expect(page.get_by_test_id("tally")).to_have_text("For 1 · Against 1 · Abstain 0")
    page.get_by_role("button", name="Adopt resolution").click()

    page.goto(res_url)
    expect(page.locator("h1")).to_contain_text("Resolution 2026-01")
    expect(page.get_by_test_id("status")).to_contain_text("Adopted")
    expect(page.get_by_role("button", name="Save draft")).to_have_count(0)
    expect(page.get_by_role("button", name="Save votes")).to_have_count(0)
    expect(page.get_by_role("button", name="Adopt resolution")).to_have_count(0)
    expect(page.get_by_text(f"{treasurer}: For", exact=True)).to_be_visible()
    expect(page.get_by_text(f"{secretary}: Against", exact=True)).to_be_visible()
    axe(page)

    page.goto(PAGE)
    act = page.locator("form[action$='/actions/add/']")
    act.get_by_label("Title").fill("Send budget to owners")
    act.get_by_label("Owner").select_option(label=treasurer)
    act.get_by_role("button", name="Add action item").click()
    page.goto(PAGE)
    expect(page.get_by_test_id("action-items")).to_contain_text("Send budget to owners")

    page.goto("/")
    expect(page.get_by_text("Send budget to owners", exact=True)).to_have_count(0)
    ctx = browser.new_context(base_url=base_url)
    sp = ctx.new_page()
    login(BOARD_EMAIL, sp)
    sp.goto("/")
    expect(sp.get_by_text("Send budget to owners", exact=True)).to_be_visible()
    ctx.close()
    assert re.search(r"/governance/board/resolutions/\d+/$", res_url), res_url


def test_seeded_roster_and_resolution_visibility(page, browser, base_url, seed, login):
    """Demo seed: the treasurer and secretary logins are owners on the board roster;
    resolution 2025-01 (owners) is in the owner's Documents, 2025-02 (board only) is not."""
    m2.residents_enabled(True)  # Residents switched on for the portal
    login(BOARD_EMAIL)
    page.goto(PAGE)
    roster = page.get_by_test_id("roster")
    for name in ("James Okafor", "Daniel Kim", "Rafael Ortiz"):
        expect(roster).to_contain_text(name)
    expect(roster).to_contain_text("Treasurer")
    expect(roster).to_contain_text("Secretary")
    res = page.get_by_test_id("resolutions")
    expect(res).to_contain_text("2025-01")
    expect(res).to_contain_text("2025-02")

    ctx = browser.new_context(base_url=base_url)
    owner = ctx.new_page()
    login(OWNER_EMAIL, owner)
    owner.goto("/me/docs/")
    docs = owner.get_by_test_id("docs-list")
    expect(docs).to_contain_text("Resolution 2025-01: Collection policy")
    expect(docs).not_to_contain_text("2025-02")
    expect(docs).not_to_contain_text("Gutter cleaning contract")
    ctx.close()
