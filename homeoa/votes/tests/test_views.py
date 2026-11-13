"""Board vote pages, owner portal voting (post-cutover), ballots issued."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from homeoa.access.testing import close_residents, grant, open_residents
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos as todo_registry
from homeoa.letters.models import Letter, MailBatch
from homeoa.notify.models import Notification
from homeoa.registry import services as registry
from homeoa.registry.models import NoticePreference, Occupancy, Person, Unit
from homeoa.votes import services
from homeoa.votes.models import Ballot, Channel, Vote, VoteKind, VoteStatus

User = get_user_model()
UNITS = ("A", "B", "C")


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(
        name="Cedar Row",
        short_name="CR",
        timezone="America/Los_Angeles",
    )
    open_residents(a)  # post-cutover
    with association_context(a):
        yield a


@pytest.fixture
def owners(assoc):
    out = {}
    for n in UNITS:
        unit = Unit.objects.create(number=n, allocated_interest=Decimal("33.3333"))
        email = f"owner{n.lower()}@example.org"
        usr = User.objects.create_user(email, password="x-pass-12345")
        person = Person.objects.create(full_name=f"Owner {n}", email=email, user=usr)
        registry.set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
        out[n] = (usr, person, unit)
    return out


@pytest.fixture
def board(assoc):
    u = User.objects.create_user("board@example.org", password="x-pass-12345")
    grant(u, "board", association=assoc)
    return u


def _vote(assoc, *, status=VoteStatus.OPEN, secret=False):
    now = timezone.now()
    vote = services.create_vote(
        assoc,
        title="Repaint the hallway",
        kind=VoteKind.WRITTEN,
        opens_at=now - dt.timedelta(days=1),
        closes_at=now + dt.timedelta(days=10),
        questions=[("Repaint?", services.yes_no_abstain())],
        secret=secret,
    )
    if status == VoteStatus.OPEN:
        services.open_vote(vote)
    return vote


def _choice(vote, label):
    q = vote.questions.get()
    return q, q.choices.get(label=label)


# --- board ---------------------------------------------------------------------------


def test_board_creates_opens_and_closes(
    client, assoc, owners, board, django_capture_on_commit_callbacks
):
    client.force_login(board)
    resp = client.post(
        "/governance/votes/add/",
        {
            "title": "Waive the audit",
            "kind": "written",
            "opens_at": "2026-11-01T09:00",
            "closes_at": "2026-11-30T17:00",
            "questions": "Waive the audit?",
            "threshold": "majority_cast",
            "quorum": "none",
        },
    )
    vote = Vote.objects.get()
    assert resp.status_code == 302 and vote.status == VoteStatus.DRAFT
    assert vote.weighting == "per_unit"
    assert client.get("/governance/votes/").status_code == 200
    with django_capture_on_commit_callbacks(execute=True):
        client.post(f"/governance/votes/{vote.pk}/open/")
    vote.refresh_from_db()
    assert vote.status == VoteStatus.OPEN
    page = client.get(f"/governance/votes/{vote.pk}/")
    assert page.status_code == 200 and "0 of 3 units voted" in page.content.decode()
    client.post(f"/governance/votes/{vote.pk}/close/")
    vote.refresh_from_db()
    assert vote.status == VoteStatus.CLOSED and vote.ballots_hash
    page = client.get(f"/governance/votes/{vote.pk}/").content.decode()
    assert "Vote closed" in page and "ballots unchanged since close" in page


def test_owner_cannot_reach_board_pages(client, assoc, owners):
    client.force_login(owners["A"][0])
    assert client.get("/governance/votes/").status_code in (302, 403)


def test_board_paper_entry(client, assoc, owners, board):
    vote = _vote(assoc)
    q, yes = _choice(vote, "Yes")
    client.force_login(board)
    _, person, unit = owners["B"]
    scan = SimpleUploadedFile("b.pdf", b"%PDF-1.4 scan", content_type="application/pdf")
    client.post(
        f"/governance/votes/{vote.pk}/paper/",
        {"unit": unit.pk, "person": person.pk, f"q{q.pk}": yes.pk, "scan": scan},
    )
    ballot = Ballot.objects.get()
    assert ballot.channel == Channel.PAPER and ballot.entered_by == board
    assert ballot.scan_file_key
    page = client.get(f"/governance/votes/{vote.pk}/").content.decode()
    assert "1 of 3 units voted" in page


def test_paper_entry_needs_scan(client, assoc, owners, board):
    vote = _vote(assoc)
    q, yes = _choice(vote, "Yes")
    client.force_login(board)
    _, person, unit = owners["B"]
    client.post(
        f"/governance/votes/{vote.pk}/paper/",
        {"unit": unit.pk, "person": person.pk, f"q{q.pk}": yes.pk},
    )
    assert not Ballot.objects.exists()


def test_secret_vote_hides_choices_from_board(client, assoc, owners, board):
    vote = _vote(assoc, secret=True)
    q, yes = _choice(vote, "Yes")
    usr, person, unit = owners["A"]
    services.cast_ballot(vote, unit, person, {q.pk: yes.pk})
    client.force_login(board)
    page = client.get(f"/governance/votes/{vote.pk}/").content.decode()
    assert "Owner A" in page
    assert "<td>Yes</td>" not in page


def test_turnout_todo(assoc, owners):
    vote = _vote(assoc)
    q, yes = _choice(vote, "Yes")
    _, person, unit = owners["A"]
    services.cast_ballot(vote, unit, person, {q.pk: yes.pk})
    items = [i for i in todo_registry.collect(assoc) if i.id == f"votes-open-{vote.pk}"]
    assert len(items) == 1
    assert items[0].title.startswith("Vote closes ") and "2 units outstanding" in items[0].title


def test_no_todo_for_draft_or_closed(assoc, owners):
    _vote(assoc, status=VoteStatus.DRAFT)
    closed = _vote(assoc)
    services.close_vote(closed)
    assert not [i for i in todo_registry.collect(assoc) if i.id.startswith("votes-open-")]


# --- issuing ballots -------------------------------------------------------------------


def test_open_emails_consenting_and_mails_the_rest(
    assoc, owners, board, django_capture_on_commit_callbacks
):
    from homeoa.votes import issue

    _, person_a, _ = owners["A"]
    registry.record_notice_preference(
        person_a, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )
    vote = _vote(assoc, status=VoteStatus.DRAFT)
    with django_capture_on_commit_callbacks(execute=True):
        services.open_vote(vote)
        issue.issue_ballots(vote, user=board)
    notes = Notification.objects.filter(type_key=issue.NOTIFICATION_TYPE)
    assert {n.recipient_id for n in notes} == {person_a.pk}
    letters = Letter.objects.all()
    assert sorted(letter.unit.number for letter in letters) == ["B", "C"]
    batch = MailBatch.objects.get()
    assert batch.items.count() == 2


def test_ballot_letter_failure_does_not_block_opening(
    assoc, owners, board, django_capture_on_commit_callbacks, monkeypatch
):
    from homeoa.audit.models import AuditEvent
    from homeoa.votes import issue

    def boom(*a, **k):
        raise RuntimeError("printer on fire")

    monkeypatch.setattr(issue, "make_letters", boom)
    vote = _vote(assoc, status=VoteStatus.DRAFT)
    with django_capture_on_commit_callbacks(execute=True):
        services.open_vote(vote)
        issue.issue_ballots(vote, user=board)
    vote.refresh_from_db()
    assert vote.status == VoteStatus.OPEN
    assert AuditEvent.objects.filter(action="votes.ballot_letters_failed").exists()


# --- owner portal ----------------------------------------------------------------------


def test_owner_votes_once_per_unit_and_changes(client, assoc, owners):
    vote = _vote(assoc)
    q, yes = _choice(vote, "Yes")
    _, no = _choice(vote, "No")
    usr, person, unit = owners["A"]
    client.force_login(usr)
    page = client.get("/me/votes/")
    assert page.status_code == 200 and "Repaint the hallway" in page.content.decode()
    resp = client.post(f"/me/votes/{vote.pk}/", {"unit": unit.pk, f"q{q.pk}": yes.pk})
    first = Ballot.objects.get()
    assert resp.status_code == 302 and resp.url.endswith(f"/receipt/{first.pk}/")
    receipt = client.get(resp.url).content.decode()
    assert "Receipt id" in receipt
    client.post(f"/me/votes/{vote.pk}/", {"unit": unit.pk, f"q{q.pk}": no.pk})
    active = Ballot.objects.filter(superseded_by__isnull=True)
    assert active.count() == 1 and Ballot.objects.count() == 2
    assert active.get().answers.get().choice == no


def test_owner_cannot_vote_for_other_unit(client, assoc, owners):
    vote = _vote(assoc)
    q, yes = _choice(vote, "Yes")
    usr, _, _ = owners["A"]
    other = owners["B"][2]
    client.force_login(usr)
    resp = client.post(f"/me/votes/{vote.pk}/", {"unit": other.pk, f"q{q.pk}": yes.pk})
    assert resp.status_code == 404 and not Ballot.objects.exists()


def test_closed_vote_rejects_portal_ballot(client, assoc, owners):
    vote = _vote(assoc)
    q, yes = _choice(vote, "Yes")
    services.close_vote(vote)
    usr, _, unit = owners["A"]
    client.force_login(usr)
    client.post(f"/me/votes/{vote.pk}/", {"unit": unit.pk, f"q{q.pk}": yes.pk})
    assert not Ballot.objects.exists()


def test_tenant_gets_403(client, assoc, owners):
    vote = _vote(assoc)
    unit = owners["C"][2]
    usr = User.objects.create_user("tenant@example.org", password="x-pass-12345")
    person = Person.objects.create(full_name="Tenant", email="tenant@example.org", user=usr)
    Occupancy.objects.create(
        unit=unit, person=person, kind=Occupancy.Kind.TENANT, start=dt.date(2024, 1, 1)
    )
    client.force_login(usr)
    assert client.get("/me/votes/").status_code == 403
    assert client.get(f"/me/votes/{vote.pk}/").status_code == 403
    assert "Owner votes" not in client.get("/me/more/").content.decode()


def test_portal_closed_before_cutover(client, assoc, owners):
    close_residents(assoc)
    vote = _vote(assoc)
    client.force_login(owners["A"][0])
    assert client.get(f"/me/votes/{vote.pk}/").status_code == 403


def test_more_page_links_votes_for_owner(client, assoc, owners):
    client.force_login(owners["A"][0])
    assert "/me/votes/" in client.get("/me/more/").content.decode()


def test_other_association_vote_is_404(client, assoc, owners):
    other = Association.objects.create(name="Other", short_name="OT")
    with association_context(other):
        foreign = _vote(other)
    client.force_login(owners["A"][0])
    assert client.get(f"/me/votes/{foreign.pk}/").status_code == 404


def test_audit_trail_shows_readable_labels(client, assoc, owners, board):
    vote = _vote(assoc, status=VoteStatus.DRAFT)
    services.open_vote(vote)
    client.force_login(board)
    page = client.get(f"/governance/votes/{vote.pk}/").content.decode()
    assert "Vote opened" in page and "votes.opened" not in page


def test_vote_pages_keep_the_governance_tabs(client, board, assoc):
    """The Governance section tabs stay visible on the votes list and a vote's own page."""
    client.force_login(board)
    vote = _vote(assoc)
    for url in ("/governance/votes/", f"/governance/votes/{vote.pk}/"):
        resp = client.get(url)
        assert resp.status_code == 200
        assert resp.context["current_hub"].id == "governance"
        on = [t["label"] for t in resp.context["subtabs"] if t["on"]]
        assert [str(label) for label in on] == ["Owner votes"]
