"""Document library, visibility, governing documents with versions."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.documents import services
from homeoa.documents.models import Document, DocumentVersion
from homeoa.registry import services as reg
from homeoa.registry.models import Occupancy, Person, Unit

User = get_user_model()
PDF = b"%PDF-1.4 test\n%%EOF\n"


@pytest.fixture(autouse=True)
def _files(settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row",
        short_name="CR",
        timezone="America/Los_Angeles",
    )
    open_residents(a)
    with association_context(a):
        yield a


@pytest.fixture
def board(assoc):
    """The Secretary: manages documents (other board members read them)."""
    u = User.objects.create_user("board@example.org")
    grant(u, association=assoc, start=dt.date(2020, 1, 1), title="secretary")
    return u


def _owner(email="o@example.org", n="A"):
    usr = User.objects.create_user(email)
    person = Person.objects.create(full_name=email, email=email, user=usr)
    u = Unit.objects.create(number=n, allocated_interest=Decimal("9"))
    reg.set_initial_ownership(u, [person], dt.date(2020, 1, 1))
    return usr, u


@pytest.fixture
def owner(assoc):
    return _owner()[0]


@pytest.fixture
def tenant(assoc):
    _, u = _owner("landlord@example.org", "E")
    usr = User.objects.create_user("t@example.org")
    person = Person.objects.create(full_name="T", email="t@example.org", user=usr)
    Occupancy.objects.create(
        unit=u, person=person, kind=Occupancy.Kind.TENANT, start=dt.date(2024, 1, 1)
    )
    return usr


@pytest.fixture
def docs(assoc):
    return {
        vis: services.attach(
            assoc, filename=f"{vis}.pdf", content=PDF, title=f"Doc {vis}", visibility=vis
        ).document
        for vis in ("public", "tenants", "owners", "board")
    }


VISIBLE = {
    "anon": {"public"},
    "tenant": {"public", "tenants"},
    "owner": {"public", "tenants", "owners"},
    "board": {"public", "tenants", "owners", "board"},
}


@pytest.mark.parametrize("who", ["anon", "tenant", "owner", "board"])
def test_visibility_matrix_list_and_download(client, assoc, docs, board, owner, tenant, who):
    user = {"anon": None, "tenant": tenant, "owner": owner, "board": board}[who]
    if user:
        client.force_login(user)
    listed = {d.visibility for d in services.visible_documents(user or _anon(), assoc)}
    assert listed == VISIBLE[who]
    for vis, doc in docs.items():
        resp = client.get(f"/documents/{doc.public_id}/download/")
        if vis in VISIBLE[who]:
            assert resp.status_code == 200, (who, vis)
            assert resp.content == PDF
            assert resp["Referrer-Policy"] == "no-referrer"
        else:
            assert resp.status_code in (302, 404), (who, vis)
            assert resp.content != PDF
    if who in ("tenant", "owner"):
        page = client.get("/me/docs/").content.decode()
        for vis in docs:
            assert (f"Doc {vis}" in page) == (vis in VISIBLE[who])


def _anon():
    from django.contrib.auth.models import AnonymousUser

    return AnonymousUser()


def test_anonymous_private_download_goes_to_login(client, docs):
    resp = client.get(f"/documents/{docs['owners'].public_id}/download/")
    assert resp.status_code == 302 and "/login/" in resp["Location"]


def test_versions_history_and_older_versions_board_only(client, assoc, board, owner):
    v1 = services.attach(
        assoc,
        filename="decl.pdf",
        content=b"%PDF v1",
        category="governing",
        governing_kind="declaration",
        visibility="tenants",
        effective_date=dt.date(1994, 5, 1),
        note="Original",
    )
    doc = v1.document
    v2 = services.attach(
        assoc,
        filename="decl-2011.pdf",
        content=b"%PDF v2",
        effective_date=dt.date(2011, 3, 1),
        note="Amended 2011",
        document=doc,
    )
    doc.refresh_from_db()
    assert doc.current_version == v2
    assert [v.number for v in doc.versions.all()] == [2, 1]
    assert [v.effective_date for v in doc.versions.all()] == [
        dt.date(2011, 3, 1),
        dt.date(1994, 5, 1),
    ]
    client.force_login(owner)
    assert client.get(f"/documents/{doc.public_id}/download/").content == b"%PDF v2"
    assert client.get(f"/documents/{doc.public_id}/v1/download/").status_code == 404
    client.force_login(board)
    assert client.get(f"/documents/{doc.public_id}/v1/download/").content == b"%PDF v1"
    page = client.get(f"/documents/{doc.public_id}/").content.decode()
    assert "Amended 2011" in page and "Original" in page
    # Versions are never deleted.
    with pytest.raises(Exception):  # noqa: B017 (ProtectedError)
        doc.delete()


def test_manage_permission_403(client, assoc, docs, owner):
    client.force_login(owner)
    doc = docs["owners"]
    assert client.get("/documents/").status_code == 403
    assert client.get("/documents/upload/").status_code == 403
    assert client.get(f"/documents/{doc.public_id}/").status_code == 403
    resp = client.post(f"/documents/{doc.public_id}/visibility/", {"visibility": "public"})
    assert resp.status_code == 403
    doc.refresh_from_db()
    assert doc.visibility == "owners"


def test_bulk_upload_and_audit(client, assoc, board):
    client.force_login(board)
    files = [
        SimpleUploadedFile("minutes-jan.pdf", PDF, content_type="application/pdf"),
        SimpleUploadedFile("minutes-feb.pdf", PDF, content_type="application/pdf"),
    ]
    resp = client.post(
        "/documents/upload/",
        {"files": files, "category": "minutes", "visibility": "owners"},
    )
    assert resp.status_code == 302
    titles = set(Document.objects.values_list("title", flat=True))
    assert titles == {"minutes-jan", "minutes-feb"}
    assert set(Document.objects.values_list("visibility", flat=True)) == {"owners"}
    events = AuditEvent.objects.filter(action="documents.uploaded")
    assert events.count() == 2 and events.first().actor_id == board.pk

    doc = Document.objects.first()
    client.post(f"/documents/{doc.public_id}/visibility/", {"visibility": "board"})
    doc.refresh_from_db()
    assert doc.visibility == "board"
    ev = AuditEvent.objects.get(action="documents.visibility_changed")
    assert ev.changes == {"visibility": ["owners", "board"]}


def test_upload_limits(client, assoc, board, settings):
    settings.HOMEOA_DOCUMENT_MAX_BYTES = 10
    client.force_login(board)
    resp = client.post(
        "/documents/upload/",
        {
            "files": [SimpleUploadedFile("x.exe", b"MZ")],
            "category": "other",
            "visibility": "board",
        },
    )
    assert resp.status_code == 200 and b"not accepted" in resp.content
    with pytest.raises(services.UploadRejected):
        services.attach(assoc, filename="big.pdf", content=b"x" * 11)
    assert not Document.objects.exists()


def test_hub_list_filter_and_search(client, assoc, board):
    services.attach(assoc, filename="b.pdf", content=PDF, title="Bylaws", category="governing")
    services.attach(assoc, filename="c.pdf", content=PDF, title="Zebra plan", category="financial")
    client.force_login(board)
    page = client.get("/documents/governing/").content.decode()
    assert "Bylaws" in page and "Zebra plan" not in page
    page = client.get("/documents/?q=zebra").content.decode()
    assert "Zebra plan" in page and "Bylaws" not in page


def test_association_isolation(client, assoc, board, docs):
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        foreign = services.attach(
            other, filename="f.pdf", content=PDF, title="Foreign", visibility="public"
        )
    assert foreign.file_key.startswith(f"{other.pk}/")
    assert not services.visible_documents(board, assoc).filter(title="Foreign").exists()
    client.force_login(board)
    assert client.get(f"/documents/{foreign.document.public_id}/download/").status_code == 404
    v = DocumentVersion.all_associations.get(pk=foreign.pk)
    assert services.can_download(board, assoc, v) is False
    with pytest.raises(services.UploadRejected):
        services.attach(assoc, filename="x.pdf", content=PDF, document=foreign.document)
