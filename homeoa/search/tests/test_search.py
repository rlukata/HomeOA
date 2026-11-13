"""Search across everything (PLT-13): one box in the top bar, results by kind,
only what the person may see, and only the features the board turned on."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant, open_residents
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.documents import services as docs
from homeoa.documents.models import Visibility
from homeoa.documents.tests.test_text import _pdf
from homeoa.jobs.testing import capture_jobs
from homeoa.plugins import modules, slots
from homeoa.reports.tests.test_board_packet import TREASURER, pilot  # noqa: F401
from homeoa.search import services as search

User = get_user_model()
OWNER_A = "ownera@cedarrow.example"


def _groups(user, association, query):
    return {g.label: [h.title for h in g.hits] for g in search.search(user, association, query)}


def _file(association, title, body, visibility):
    with capture_jobs() as queue:
        docs.attach(
            association,
            filename=f"{title}.pdf",
            content=_pdf(body),
            title=title,
            visibility=visibility,
        )
        queue.run_all()


def test_board_finds_units_people_vendors_and_bills(pilot):  # noqa: F811
    treasurer = User.objects.get(email=TREASURER)
    assert _groups(treasurer, pilot, "Unit A")["Units"][0] == "Unit A"
    # A misspelled name still finds the person.
    assert "Maya Nguyen" in _groups(treasurer, pilot, "Nguyn")["People"]
    found = _groups(treasurer, pilot, "Olympia")
    assert "City of Olympia Utilities" in found["Vendors"]


def test_document_text_found_with_the_matching_words_marked(pilot):  # noqa: F811
    treasurer = User.objects.get(email=TREASURER)
    _file(pilot, "Rules 2024", "Trash bins go out on Tuesday nights.", Visibility.OWNERS)
    (group,) = [g for g in search.search(treasurer, pilot, "trash bins") if g.label == "Documents"]
    hit = group.hits[0]
    assert hit.title == "Rules 2024" and "page 1" in hit.detail
    assert "<mark>Trash</mark>" in hit.snippet


def test_owners_only_find_what_they_may_see(pilot):  # noqa: F811
    open_residents(pilot)
    owner = User.objects.get(email=OWNER_A)
    _file(pilot, "Collections memo", "Payment plan for a delinquent unit.", Visibility.BOARD)
    _file(pilot, "Pool rules", "Payment plan brochure for owners.", Visibility.OWNERS)
    found = _groups(owner, pilot, "payment plan")
    assert found == {"Documents": ["Pool rules"]}
    assert _groups(owner, pilot, "Nguyen") == {}  # no people or units for owners


@pytest.fixture
def fresh(db):
    a = Association.objects.create(name="Birch Court", short_name="BC", timezone="UTC")
    with association_context(a):
        yield a


def test_features_turned_off_are_not_searched(fresh, settings):
    from homeoa.enforcement.models import Violation, ViolationCategory
    from homeoa.registry.models import Unit

    settings.HOMEOA_MODULES_DEFAULT_ON = False  # a new association starts with them off
    board = User.objects.create_user("p@example.org")
    grant(board, association=fresh, start=dt.date(2020, 1, 1), title="president")
    category = ViolationCategory.objects.create(name="Trash bins", rule_citation="Rule 4")
    Violation.objects.create(
        unit=Unit.objects.create(number="B"),
        category=category,
        observed_on=dt.date(2026, 10, 1),
        description="Bins left out all week",
    )
    assert not modules.is_on(fresh, "violations")
    assert "Violations" not in _groups(board, fresh, "bins")
    modules.set_module(fresh, "violations", True, user=board)
    assert _groups(board, fresh, "bins")["Violations"] == ["Trash bins · Unit B"]


def test_a_failing_source_leaves_the_others(pilot, monkeypatch):  # noqa: F811
    def broken(user, association, query):
        raise RuntimeError("boom")

    contributions = [*slots.contributions(slots.SEARCH_SOURCES)]
    contributions.insert(0, slots.Contribution(slots.SEARCH_SOURCES, broken))
    monkeypatch.setitem(slots._contributions, slots.SEARCH_SOURCES, contributions)
    treasurer = User.objects.get(email=TREASURER)
    assert "Units" in _groups(treasurer, pilot, "Unit A")


def test_search_page_from_the_top_bar(pilot, client):  # noqa: F811
    client.force_login(User.objects.get(email=TREASURER))
    home = client.get("/").content.decode()
    assert 'data-testid="search-button"' in home
    html = client.get("/search/", {"q": "Unit A"}).content.decode()
    assert 'data-testid="search-hit"' in html and "/units/A/" in html
    assert "Type at least 2 characters." in client.get("/search/", {"q": "A"}).content.decode()
    assert "Nothing found for" in client.get("/search/", {"q": "zzqx"}).content.decode()
