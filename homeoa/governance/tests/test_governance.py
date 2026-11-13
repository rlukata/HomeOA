"""Board roster, committees, resolutions and action items (GOV-02, GOV-06)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.governance import services
from homeoa.governance.models import (
    ActionItem,
    Committee,
    ImmutableResolutionError,
    Resolution,
    ResolutionVote,
)
from homeoa.registry.models import Person

D = dt.date


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def person(a, name, user=None):
    return Person.objects.create(association=a, full_name=name, user=user)


def user(email):
    return get_user_model().objects.create_user(email=email, password="x-pass-12345")


def gov_todos(a, u=None):
    return [t for t in todos.collect(a, u) if t.source == "governance"]


def test_roster_on_date(assoc):
    ann, bob = person(assoc, "Ann"), person(assoc, "Bob")
    services.add_term(
        assoc, person=ann, office="president", start=D(2025, 1, 1), end=D(2026, 12, 31)
    )
    services.add_term(assoc, person=bob, office="treasurer", start=D(2027, 1, 1))
    assert [t.person for t in services.roster(assoc, D(2026, 6, 1))] == [ann]
    assert [t.person for t in services.roster(assoc, D(2027, 1, 1))] == [bob]
    assert services.roster(assoc, D(2024, 12, 31)) == []
    assert [t.person for t in services.roster(assoc, D(2026, 12, 31))] == [ann]


def test_term_end_before_start_refused(assoc):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        services.add_term(
            assoc,
            person=person(assoc, "Ann"),
            office="secretary",
            start=D(2026, 1, 1),
            end=D(2025, 1, 1),
        )


def test_term_end_todo_window(assoc, monkeypatch):
    ann = person(assoc, "Ann")
    services.add_term(
        assoc, person=ann, office="president", start=D(2025, 1, 1), end=D(2026, 12, 31)
    )
    today = {"d": D(2026, 10, 31)}
    monkeypatch.setattr("homeoa.governance.todos.association_today", lambda a: today["d"])
    assert gov_todos(assoc) == []  # 61 days out
    today["d"] = D(2026, 11, 1)  # 60 days out
    items = gov_todos(assoc)
    assert len(items) == 1 and items[0].due == D(2026, 12, 31)
    today["d"] = D(2027, 1, 1)
    assert gov_todos(assoc) == []
    # The window is a setting.
    assoc.settings = {services.TERM_END_SETTING: 90}
    assoc.save()
    today["d"] = D(2026, 10, 15)
    assert len(gov_todos(assoc)) == 1


def test_committees_with_chair(assoc):
    c = Committee.objects.create(association=assoc, name="Landscape")
    ann, bob = person(assoc, "Ann"), person(assoc, "Bob")
    services.add_committee_member(c, person=ann, start=D(2026, 1, 1), is_chair=True)
    services.add_committee_member(c, person=bob, start=D(2026, 1, 1), end=D(2026, 3, 1))
    [row] = services.committees(assoc, D(2026, 6, 1))
    assert row["chair"].person == ann and [m.person for m in row["members"]] == [ann]


def test_resolution_numbering_per_year_and_association(assoc):
    r1 = services.create_resolution(assoc, title="A", year=2026)
    r2 = services.create_resolution(assoc, title="B", year=2026)
    r3 = services.create_resolution(assoc, title="C", year=2027)
    assert (r1.number, r2.number, r3.number) == ("2026-01", "2026-02", "2027-01")
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert services.create_resolution(other, title="X", year=2026).number == "2026-01"


def test_adopted_resolution_immutable_except_rescind(assoc):
    ann = person(assoc, "Ann")
    services.add_term(assoc, person=ann, office="president", start=D(2025, 1, 1))
    r = services.create_resolution(assoc, title="Pet policy", text="Dogs on leash.", year=2026)
    services.record_votes(r, {ann: "for"})
    assert services.tally(r) == {"for": 1, "against": 0, "abstain": 0}
    pdf = SimpleUploadedFile("res.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    services.adopt(r, adopted_on=D(2026, 3, 1), pdf=pdf, visibility="owners")
    r.refresh_from_db()
    assert r.status == "adopted" and r.pdf_key and r.pdf_visibility == "owners"
    doc = r.document
    assert doc.visibility == "owners" and doc.category == "governing"
    assert doc.governing_kind == "resolution" and "2026-01" in doc.title
    assert doc.current_version.file_key == r.pdf_key
    assert doc.current_version.effective_date == D(2026, 3, 1)

    with pytest.raises(services.GovernanceError):
        services.update_draft(r, title="X", text="Y")
    r.title = "Changed"
    with pytest.raises(ImmutableResolutionError):
        r.save()
    r.refresh_from_db()
    with pytest.raises(ImmutableResolutionError):
        ResolutionVote.objects.get(resolution=r).delete()
    with pytest.raises(services.GovernanceError):
        services.record_votes(r, {ann: "against"})
    with pytest.raises(ImmutableResolutionError):
        r.delete()

    services.rescind(r, on=D(2026, 9, 1), reason="Superseded")
    r.refresh_from_db()
    assert r.status == "rescinded" and r.rescinded_on == D(2026, 9, 1)
    assert AuditEvent.objects.filter(action="governance.resolution_rescinded").exists()
    with pytest.raises(services.GovernanceError):
        services.rescind(r, on=D(2026, 9, 2), reason="again")
    r.rescind_reason = "edited"
    with pytest.raises(ImmutableResolutionError):
        r.save()


def test_adopt_rejects_non_pdf(assoc):
    r = services.create_resolution(assoc, title="A", year=2026)
    bad = SimpleUploadedFile("x.docx", b"data", content_type="application/msword")
    with pytest.raises(services.GovernanceError):
        services.adopt(r, adopted_on=D(2026, 1, 2), pdf=bad)


def test_adopted_pdf_filed_at_board_visibility_and_audited(assoc):
    from homeoa.documents.models import Document

    u = user("sec@example.com")
    r = services.create_resolution(assoc, title="Exec matter", year=2026)
    pdf = SimpleUploadedFile("signed", b"%PDF-1.4 x", content_type="application/pdf")
    services.adopt(r, adopted_on=D(2026, 4, 1), pdf=pdf, visibility="board", user=u)
    r.refresh_from_db()
    doc = Document.objects.get(pk=r.document_id)
    assert doc.visibility == "board"
    assert doc.current_version.file_name == "signed.pdf"
    assert doc.current_version.uploaded_by == u
    assert AuditEvent.objects.filter(action="documents.uploaded").exists()


def test_adopt_without_pdf_files_nothing(assoc):
    from homeoa.documents.models import Document

    r = services.create_resolution(assoc, title="Verbal", year=2026)
    services.adopt(r, adopted_on=D(2026, 4, 1))
    r.refresh_from_db()
    assert r.document_id is None and not r.pdf_key
    assert not Document.objects.exists()


def test_action_item_todo_only_for_assignee(assoc):
    u1, u2 = user("a@example.com"), user("b@example.com")
    grant(u1, association=assoc)
    grant(u2, association=assoc)
    ann, bob = person(assoc, "Ann", u1), person(assoc, "Bob", u2)
    for p in (ann, bob):
        services.add_term(assoc, person=p, office="director", start=D(2025, 1, 1))
    item = services.add_action_item(
        assoc, title="Get roof bids", owner=ann, due_date=D(2026, 11, 1), on=D(2026, 10, 1)
    )
    assert [t.title for t in gov_todos(assoc, u1)] == ["Get roof bids"]
    assert gov_todos(assoc, u2) == []
    services.set_action_status(item, "done", on=D(2026, 10, 5))
    assert gov_todos(assoc, u1) == []
    assert ActionItem.objects.get().completed_on == D(2026, 10, 5)


def test_action_item_owner_must_be_board_member(assoc):
    with pytest.raises(services.GovernanceError):
        services.add_action_item(assoc, title="X", owner=person(assoc, "Zed"), on=D(2026, 1, 1))


@pytest.fixture
def board_client(client, assoc):
    """The Secretary keeps the roster, resolutions and action items."""
    u = user("board@example.com")
    grant(u, association=assoc, title="secretary")
    client.force_login(u)
    client.user = u
    return client


def test_pages_and_flow(board_client, assoc):
    ann = person(assoc, "Ann Board", board_client.user)
    resp = board_client.post(
        "/governance/board/terms/add/",
        {"person": ann.pk, "office": "president", "start": "2020-01-01"},
    )
    assert resp.status_code == 302
    resp = board_client.get("/governance/board/")
    assert resp.status_code == 200 and b"Ann Board" in resp.content
    board_client.post("/governance/board/committees/add/", {"name": "Landscape"})
    c = Committee.objects.get()
    board_client.post(
        f"/governance/board/committees/{c.pk}/members/add/",
        {"person": ann.pk, "start": "2020-01-01", "is_chair": "on"},
    )
    assert c.members.get().is_chair
    resp = board_client.post("/governance/board/resolutions/add/", {"title": "Pets"})
    r = Resolution.objects.get()
    assert resp.url == f"/governance/board/resolutions/{r.pk}/"
    act = f"/governance/board/resolutions/{r.pk}/act/"
    board_client.post(act, {"action": "votes", f"vote-{ann.pk}": "for"})
    assert services.tally(r)["for"] == 1
    pdf = SimpleUploadedFile("r.pdf", b"%PDF-1.4", content_type="application/pdf")
    board_client.post(
        act, {"action": "adopt", "adopted_on": "2026-03-01", "pdf": pdf, "visibility": "board"}
    )
    r.refresh_from_db()
    assert r.status == "adopted"
    page = board_client.get(f"/governance/board/resolutions/{r.pk}/")
    assert page.status_code == 200
    assert "Ann Board: For" in page.content.decode()
    assert board_client.get(f"/governance/board/resolutions/{r.pk}/pdf/").content == b"%PDF-1.4"
    board_client.post(
        "/governance/board/actions/add/",
        {"title": "Notify owners", "owner": ann.pk, "resolution": r.pk},
    )
    item = ActionItem.objects.get()
    board_client.post(f"/governance/board/actions/{item.pk}/status/", {"status": "done"})
    item.refresh_from_db()
    assert item.status == "done"


def test_owner_cannot_read_or_manage(client, assoc):
    u = user("owner@example.com")
    client.force_login(u)
    assert client.get("/governance/board/").status_code in (302, 403)
    resp = client.post("/governance/board/resolutions/add/", {"title": "X"})
    assert resp.status_code in (302, 403)
    assert not Resolution.objects.exists()


def test_rescind_marks_filed_document(assoc):
    """Rescinding marks the library document rescinded with a note and link."""
    u = user("sec2@example.com")
    r = services.create_resolution(assoc, title="Old rule", year=2026)
    pdf = SimpleUploadedFile("r.pdf", b"%PDF-1.4 x", content_type="application/pdf")
    services.adopt(r, adopted_on=D(2026, 2, 1), pdf=pdf, visibility="owners")
    services.rescind(r, on=D(2026, 5, 1), reason="Replaced", user=u)
    doc = r.document
    doc.refresh_from_db()
    assert doc.status == "rescinded"
    assert "Replaced" in doc.status_note and "2026-05-01" in doc.status_note
    assert doc.status_link == f"/governance/board/resolutions/{r.pk}/"
    ev = AuditEvent.objects.get(action="documents.status_changed")
    assert ev.actor == u


def test_rescind_without_pdf_is_fine(assoc):
    r = services.create_resolution(assoc, title="Verbal", year=2026)
    services.adopt(r, adopted_on=D(2026, 2, 1))
    services.rescind(r, on=D(2026, 3, 1), reason="No longer needed")
    assert r.status == "rescinded"


def test_voter_listed_once_with_two_terms(board_client, assoc):
    ann = person(assoc, "Ann Twohats", board_client.user)
    services.add_term(assoc, person=ann, office="president", start=D(2025, 1, 1))
    services.add_term(assoc, person=ann, office="treasurer", start=D(2025, 1, 1))
    r = services.create_resolution(assoc, title="Dedupe", year=2026)
    page = board_client.get(f"/governance/board/resolutions/{r.pk}/")
    assert page.status_code == 200
    assert [v["person"].pk for v in page.context["voters"]] == [ann.pk]


# --- contact list and messages ---------------------------------------------------


@pytest.fixture
def served(assoc):
    """A president, a treasurer and a two-person Maintenance committee, as of today."""
    from homeoa.governance.models import Committee, Office

    start = dt.date(2025, 1, 1)
    pres = person(assoc, "Rafael Ortiz")
    pres.email = "rafael@example.org"
    pres.save()
    services.add_term(assoc, person=pres, office=Office.PRESIDENT, start=start)
    treas = person(assoc, "James Okafor")
    treas.email = "james@example.org"
    treas.save()
    services.add_term(assoc, person=treas, office=Office.TREASURER, start=start)
    maint = Committee.objects.create(association=assoc, name="Maintenance committee")
    elena = person(assoc, "Elena Petrova")
    elena.email = "elena@example.org"
    elena.save()
    services.add_committee_member(maint, person=elena, start=start, is_chair=True)
    return {"committee": maint}


def _resident(assoc, email="tenant@example.org"):
    """A tenant with portal access (residents switched on)."""
    from decimal import Decimal

    from homeoa.registry import services as reg
    from homeoa.registry.models import Unit

    open_residents(assoc)
    u = user(email)
    unit = Unit.objects.create(number="E", allocated_interest=Decimal("10"))
    owner = person(assoc, "Owner E")
    reg.set_initial_ownership(unit, [owner], dt.date(2020, 1, 1))
    tenant = person(assoc, "Chris Doyle", user=u)
    reg.add_occupancy(unit, tenant, dt.date(2025, 6, 1))
    return u


def test_residents_see_who_serves_but_not_their_emails(client, assoc, served):
    client.force_login(_resident(assoc))
    html = client.get("/me/contacts/").content.decode()
    assert "Rafael Ortiz" in html and "President" in html
    assert "Elena Petrova" in html and "Maintenance committee" in html
    assert "rafael@example.org" not in html and "elena@example.org" not in html
    assert "/me/contacts/" in client.get("/me/more/").content.decode()


def test_message_to_a_committee_emails_its_members(
    client, assoc, served, django_capture_on_commit_callbacks
):
    from homeoa.governance.models import ContactMessage
    from homeoa.notify.models import Notification

    client.force_login(_resident(assoc))
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            "/me/contacts/",
            {
                "to": f"committee:{served['committee'].pk}",
                "subject": "Leaky gutter",
                "body": "Over unit E.",
            },
        )
    assert resp.status_code == 302
    message = ContactMessage.objects.get()
    assert message.to_label == "Maintenance committee" and message.recipient_count == 1
    notes = Notification.objects.filter(type_key="governance.contact_message")
    assert [n.recipient.full_name for n in notes] == ["Elena Petrova"]


def test_board_inbox_lists_messages(client, assoc, served):
    from homeoa.governance.models import ContactMessage

    sender = user("o@example.org")
    ContactMessage.objects.create(
        association=assoc, sender=sender, to="board", subject="Parking", body="Spot 4"
    )
    board_user = user("b@example.org")
    grant(board_user, association=assoc)
    client.force_login(board_user)
    html = client.get("/community/messages/").content.decode()
    assert "Parking" in html and "o@example.org" in html
    owner = user("plain@example.org")
    client.force_login(owner)
    assert client.get("/community/messages/").status_code == 403
