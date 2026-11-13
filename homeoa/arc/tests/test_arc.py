"""Architectural review forms, submission, comments, votes and decisions."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from homeoa.access.testing import close_residents, grant, open_residents
from homeoa.arc import services
from homeoa.arc.models import ArcComment, ArcRequest, ArcVote
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.governance import services as gov
from homeoa.notify.models import Notification
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, Unit

User = get_user_model()
D = dt.date


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(
        name="Cedar Row",
        short_name="CR",
        timezone="America/Los_Angeles",
    )
    open_residents(a)
    with association_context(a):
        for n in "ABCDEFGHI":
            Unit.objects.create(number=n, allocated_interest=Decimal("11.1111"))
        yield a


def unit(n):
    return Unit.objects.get(number=n)


def owner(assoc, n="A", email="owner-a@example.org"):
    usr = User.objects.create_user(email=email, password="x-pass-12345")
    p = Person.objects.create(full_name=email, email=email, user=usr)
    reg.set_initial_ownership(unit(n), [p], D(2020, 1, 1))
    return usr, p


def board(assoc, email, office="director"):
    usr = User.objects.create_user(email=email, password="x-pass-12345")
    grant(usr, "board", association=assoc)
    p = Person.objects.create(full_name=email, email=email, user=usr)
    gov.add_term(assoc, person=p, office=office, start=D(2020, 1, 1))
    return usr, p


GOOD = {
    "description": "Paint the front door blue",
    "location": "Front door",
    "category": "Paint or color",
}


def make_request(assoc, person, n="A", **data):
    form = services.ensure_default_form(assoc)
    return services.submit(
        assoc, unit=unit(n), person=person, form=form, title="Blue door", data={**GOOD, **data}
    )


# ------------------------------------------------------------------ form definitions


def test_default_form_seeded_once(assoc):
    f = services.ensure_default_form(assoc)
    assert services.ensure_default_form(assoc) == f
    assert {x["kind"] for x in f.fields} == {"text", "textarea", "date", "choice", "checkbox"}


def test_validate_fields_rules(assoc):
    with pytest.raises(services.ArcError):
        services.validate_fields([])
    with pytest.raises(services.ArcError):
        services.validate_fields([{"label": "Color", "kind": "choice", "choices": ["Red"]}])
    with pytest.raises(services.ArcError):
        services.validate_fields([{"label": "X", "kind": "file"}])
    out = services.validate_fields(
        [{"label": "Color", "kind": "text"}, {"label": "Color", "kind": "text"}]
    )
    assert [f["key"] for f in out] == ["color", "color_2"]


def test_clean_answers_from_definition(assoc):
    defs = services.default_form_fields()
    answers, errors = services.clean_answers(defs, {})
    assert set(errors) == {"description", "location", "category"}
    answers, errors = services.clean_answers(
        defs, {**GOOD, "start_date": "2026-13-40", "category": "Nope"}
    )
    assert set(errors) == {"start_date", "category"}
    answers, errors = services.clean_answers(
        defs, {**GOOD, "start_date": "2026-11-02", "neighbors": "on"}
    )
    assert errors == {}
    assert answers["start_date"] == "2026-11-02" and answers["neighbors"] is True


def test_submit_rejects_invalid(assoc):
    _, p = owner(assoc)
    with pytest.raises(services.ArcError) as exc:
        make_request(assoc, p, location="")
    assert "location" in exc.value.errors
    assert not ArcRequest.objects.exists()


def test_form_snapshot_kept_after_edit(assoc):
    _, p = owner(assoc)
    req = make_request(assoc, p)
    form = req.form
    services.save_form(assoc, name=form.name, fields=[{"label": "Only", "kind": "text"}], form=form)
    req.refresh_from_db()
    assert len(req.form_fields) == 6


# ------------------------------------------------------------------ votes and reviewers


def test_votes_counted_and_majority(assoc):
    _, p = owner(assoc)
    _, b1 = board(assoc, "b1@example.org", "president")
    _, b2 = board(assoc, "b2@example.org", "secretary")
    _, b3 = board(assoc, "b3@example.org", "treasurer")
    req = make_request(assoc, p)
    services.cast_vote(req, reviewer=b1, vote="approve")
    t = services.tally(req)
    assert (t["in"], t["of"]) == (1, 3)
    services.cast_vote(req, reviewer=b2, vote="approve")
    services.cast_vote(req, reviewer=b3, vote="deny")
    services.cast_vote(req, reviewer=b3, vote="abstain")  # change, not a second vote
    req.refresh_from_db()
    t = services.tally(req)
    assert t["in"] == 3 and t["majority"] == "Approve"
    assert req.status == ArcRequest.Status.UNDER_REVIEW  # no auto-decide
    with pytest.raises(services.ArcError):
        services.cast_vote(req, reviewer=p, vote="approve")  # not a reviewer


def test_committee_reviewers(assoc):
    _, b1 = board(assoc, "b1@example.org")
    member = Person.objects.create(full_name="Arc Member")
    c = gov.Committee.objects.create(name="ARC")
    gov.add_committee_member(c, person=member, start=D(2020, 1, 1))
    assert services.reviewers(assoc) == [b1]
    services.set_reviewer_committee(assoc, c)
    assert services.reviewers(assoc) == [member]


def test_todo_per_reviewer_disappears_after_vote(assoc):
    _, p = owner(assoc)
    u1, b1 = board(assoc, "b1@example.org")
    u2, _b2 = board(assoc, "b2@example.org")
    req = make_request(assoc, p)

    def mine(u):
        return [t for t in todos.collect(assoc, u) if t.source == "arc"]

    assert len(mine(u1)) == 1 and len(mine(u2)) == 1
    assert "Vote on ARC request Unit A" in mine(u1)[0].title
    services.cast_vote(req, reviewer=b1, vote="approve")
    assert mine(u1) == [] and len(mine(u2)) == 1
    services.decide(req, outcome="approved", user=u2)
    assert mine(u2) == []


# ------------------------------------------------------------------ lifecycle


def test_decide_requires_conditions(assoc):
    _, p = owner(assoc)
    u, _ = board(assoc, "b1@example.org")
    req = make_request(assoc, p)
    with pytest.raises(services.ArcError):
        services.decide(req, outcome="approved_with_conditions", user=u)
    services.decide(req, outcome="approved_with_conditions", conditions="Use HOA color", user=u)
    req.refresh_from_db()
    assert req.decided_on and req.conditions == "Use HOA color"
    with pytest.raises(services.ArcError):
        services.decide(req, outcome="denied", user=u)


def test_request_info_notifies_on_commit(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    u, _ = board(assoc, "b1@example.org")
    req = make_request(assoc, p)
    with django_capture_on_commit_callbacks(execute=True):
        services.request_info(req, message="Send a color swatch", user=u)
    req.refresh_from_db()
    assert req.status == ArcRequest.Status.INFO_REQUESTED
    note = Notification.objects.get(type_key=services.INFO_REQUESTED)
    assert note.recipient == p and "color swatch" in note.body
    services.provide_info(req, message="Attached", user=None)
    req.refresh_from_db()
    assert req.status == ArcRequest.Status.UNDER_REVIEW


def test_request_info_rollback_sends_nothing(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    u, _ = board(assoc, "b1@example.org")
    req = make_request(assoc, p)
    with (
        django_capture_on_commit_callbacks(execute=True) as cbs,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        services.request_info(req, message="Swatch?", user=u)
        raise RuntimeError
    assert cbs == []
    assert not Notification.objects.filter(type_key=services.INFO_REQUESTED).exists()


def test_changes_audited(assoc):
    _, p = owner(assoc)
    u, b = board(assoc, "b1@example.org")
    req = make_request(assoc, p)
    services.cast_vote(req, reviewer=b, vote="approve")
    services.add_comment(req, body="Fine", user=u)
    services.decide(req, outcome="approved", user=u)
    models = set(AuditEvent.objects.values_list("object_type", flat=True))
    assert any("arcrequest" in m for m in models)
    assert any("arcvote" in m for m in models)
    assert any("arccomment" in m for m in models)


def test_scoped_per_association(assoc):
    _, p = owner(assoc)
    make_request(assoc, p)
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert ArcRequest.objects.count() == 0
    assert ArcRequest.objects.count() == 1


# ------------------------------------------------------------------ views


def test_tenant_cannot_submit(assoc, client):
    owner(assoc, "B", "landlord@example.org")
    usr = User.objects.create_user(email="t@example.org", password="x-pass-12345")
    tp = Person.objects.create(full_name="Tenant", user=usr)
    Occupancy.objects.create(unit=unit("B"), person=tp, start=D(2024, 1, 1))
    client.force_login(usr)
    assert client.get("/me/arc/new/").status_code == 403
    r = client.post("/me/arc/new/", {**GOOD, "title": "x", "submit": "1"})
    assert r.status_code == 403
    assert not ArcRequest.objects.exists()


def test_resident_gate_closed(assoc, client):
    usr, _ = owner(assoc)
    close_residents(assoc)
    client.force_login(usr)
    r = client.get("/me/arc/new/")
    assert r.status_code == 403 and not ArcRequest.objects.exists()


def test_owner_submits_with_attachment(assoc, client):
    usr, p = owner(assoc)
    client.force_login(usr)
    f = SimpleUploadedFile("door.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    r = client.post("/me/arc/new/", {**GOOD, "title": "Blue door", "submit": "1", "files": f})
    req = ArcRequest.objects.get()
    assert r.status_code == 302 and req.unit == unit("A") and req.submitted_by == p
    assert req.attachments.count() == 1
    assert req.attachments.get().version.document.visibility == "board"
    att = req.attachments.get()
    assert client.get(f"/me/arc/{req.pk}/files/{att.pk}/").status_code == 200


def test_internal_comments_hidden_from_requester(assoc, client):
    usr, p = owner(assoc)
    u, _ = board(assoc, "b1@example.org")
    req = make_request(assoc, p)
    services.add_comment(req, body="SECRET reviewer note", user=u, internal=True)
    services.add_comment(req, body="Visible question", user=u, internal=False)
    client.force_login(usr)
    page = client.get(f"/me/arc/{req.pk}/").content.decode()
    assert "Visible question" in page and "SECRET" not in page
    # another owner cannot see it at all
    other, _ = owner(assoc, "C", "owner-c@example.org")
    client.force_login(other)
    assert client.get(f"/me/arc/{req.pk}/").status_code == 404


def test_board_submits_on_behalf_and_reviews(assoc, client):
    _, p = owner(assoc)
    u, _ = board(assoc, "b1@example.org")
    board(assoc, "b2@example.org")
    client.force_login(u)
    form = services.ensure_default_form(assoc)
    r = client.post(
        "/requests/architectural/new/",
        {
            **GOOD,
            "unit": unit("A").pk,
            "form": form.pk,
            "person": p.pk,
            "title": "Deck",
            "submit": "1",
        },
    )
    req = ArcRequest.objects.get()
    assert r.status_code == 302 and req.entered_by == u
    client.post(f"/requests/architectural/{req.pk}/act/", {"action": "vote", "vote": "approve"})
    page = client.get(f"/requests/architectural/{req.pk}/").content.decode()
    assert "1 of 2 votes in" in page
    client.post(
        f"/requests/architectural/{req.pk}/act/",
        {"action": "decide", "outcome": "denied", "note": "Too large"},
    )
    req.refresh_from_db()
    assert req.status == "denied" and ArcVote.objects.count() == 1


def test_form_builder(assoc, client):
    u, _ = board(assoc, "b1@example.org")
    client.force_login(u)
    r = client.post(
        "/requests/architectural/forms/new/",
        {
            "name": "Satellite dish",
            "active": "on",
            "key": ["", "", ""],
            "label": ["Location", "Size", ""],
            "kind": ["text", "choice", "text"],
            "choices": ["", "Small, Large", ""],
            "required": ["0"],
        },
    )
    assert r.status_code == 302
    from homeoa.arc.models import ArcForm

    f = ArcForm.objects.get(name="Satellite dish")
    assert f.fields[0]["required"] and not f.fields[1]["required"]
    assert f.fields[1]["choices"] == ["Small", "Large"]


def test_owner_cannot_reach_board_pages(assoc, client):
    usr, _ = owner(assoc)
    client.force_login(usr)
    assert client.get("/requests/architectural/").status_code == 403
    assert ArcComment.objects.count() == 0
