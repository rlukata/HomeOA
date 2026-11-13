"""Violation categories, fine schedules and the violation log (VIO-01, VIO-02)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.documents import services as documents
from homeoa.documents.models import Visibility
from homeoa.enforcement import services
from homeoa.enforcement.models import (
    FineSchedule,
    FineScheduleLine,
    Violation,
    ViolationCategory,
)
from homeoa.registry.models import Unit

D = dt.date
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def user(email):
    return get_user_model().objects.create_user(email=email, password="x-pass-12345")


def unit(a, n="A"):
    return Unit.objects.get_or_create(
        association=a, number=n, defaults={"allocated_interest": Decimal("11.11")}
    )[0]


def category(a, name="Pets"):
    return ViolationCategory.objects.get_or_create(
        association=a, name=name, defaults={"rule_citation": "Rules §4.2"}
    )[0]


def schedule(a, version, effective, adopted=None, furnished=None):
    return FineSchedule.objects.create(
        association=a,
        version=version,
        effective_on=effective,
        adopted_on=adopted or effective,
        furnished_on=furnished,
    )


def log(a, **kw):
    kw.setdefault("unit", unit(a))
    kw.setdefault("category", category(a))
    kw.setdefault("observed_on", D(2026, 9, 1))
    kw.setdefault("description", "Dog off leash")
    kw.setdefault("photos", [("dog.png", PNG)])
    return services.log_violation(a, **kw)


def test_scoping_per_association(assoc):
    log(assoc)
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert Violation.objects.count() == 0
        assert ViolationCategory.objects.count() == 0
    assert Violation.objects.count() == 1


def test_log_refuses_foreign_unit(assoc):
    other = Association.objects.create(name="Other", short_name="O")
    with pytest.raises(ValidationError):
        log(assoc, unit=unit(other, "Z"))


def test_schedule_picked_by_date(assoc):
    v1 = schedule(assoc, 1, D(2025, 1, 1))
    v2 = schedule(assoc, 2, D(2026, 7, 1))
    assert services.schedule_in_force(assoc, D(2024, 12, 31)) is None
    assert services.schedule_in_force(assoc, D(2026, 6, 30)) == v1
    assert services.schedule_in_force(assoc, D(2026, 7, 1)) == v2
    assert services.next_schedule_version(assoc) == 3


def test_furnished_flag(assoc):
    s = schedule(assoc, 1, D(2026, 1, 1), adopted=D(2025, 12, 1))
    assert not s.is_furnished and not s.furnished_by(D(2026, 2, 1))
    s.furnished_on = D(2026, 1, 15)
    s.save()
    assert s.is_furnished
    assert not s.furnished_by(D(2026, 1, 14))
    assert s.furnished_by(D(2026, 1, 15))
    s.furnished_on = D(2025, 11, 1)  # before adoption
    with pytest.raises(ValidationError):
        s.full_clean(exclude=["association"])


def test_line_validation(assoc):
    s = schedule(assoc, 1, D(2026, 1, 1))
    line = FineScheduleLine(
        association=assoc,
        schedule=s,
        category=category(assoc),
        first_amount=Decimal("50.00"),
        repeat_amount=Decimal("100.00"),
        continuing_amount=Decimal("10.00"),
    )
    with pytest.raises(ValidationError):
        line.full_clean()  # continuing amount without a period
    line.continuing_period_days = 7
    line.full_clean()
    line.first_amount = Decimal("-1")
    with pytest.raises(ValidationError):
        line.full_clean()


def test_photo_upload_board_only(assoc):
    v = log(assoc)
    photo = v.photos.get()
    assert photo.document.visibility == Visibility.BOARD
    board, committee = user("b@x.org"), user("c@x.org")
    grant(board, association=assoc)
    grant(committee, "committee", association=assoc)
    version = photo.document.current_version
    assert documents.can_download(board, assoc, version)
    assert not documents.can_download(committee, assoc, version)


def test_photo_required_and_images_only(assoc):
    with pytest.raises(ValidationError):
        log(assoc, photos=[])
    with pytest.raises(ValidationError):
        log(assoc, photos=[("notes.pdf", b"%PDF-1.4")])
    assert Violation.objects.count() == 0


def test_photo_size_limit(assoc, settings):
    settings.HOMEOA_DOCUMENT_MAX_BYTES = 10
    with pytest.raises(ValidationError):
        log(assoc)


def test_status_change_audited(assoc):
    v = log(assoc)
    assert services.set_status(v, "cured", note="Leash used")
    assert not services.set_status(v, "cured")
    ev = AuditEvent.objects.get(action="enforcement.status_changed")
    assert ev.changes == {"status": ["open", "cured"]}
    with pytest.raises(ValidationError):
        services.set_status(v, "bogus")


def test_crud_audited(assoc):
    c = category(assoc)
    c.rule_citation = "Rules §4.3"
    c.save()
    schedule(assoc, 1, D(2026, 1, 1))
    types = set(AuditEvent.objects.values_list("object_type", flat=True))
    assert any("violationcategory" in t for t in types)
    assert any("fineschedule" in t for t in types)


def test_open_violations_todo(assoc):
    def items():
        return [t for t in todos.collect(assoc, None) if t.source == "enforcement"]

    assert items() == []
    v = log(assoc)
    log(assoc, unit=unit(assoc, "B"), category=v.category)
    assert "2 open violations" in items()[0].title
    services.set_status(v, "closed")
    assert "1 open violation" in items()[0].title


@pytest.fixture
def board_client(client, assoc):
    u = user("board@x.org")
    grant(u, association=assoc)
    client.force_login(u)
    return client


@pytest.mark.parametrize("role", ["cpa", "committee"])
def test_permissions_non_board(client, assoc, role):
    u = user(f"{role}@x.org")
    grant(u, role, association=assoc)
    client.force_login(u)
    v = log(assoc)
    for url in (
        "/requests/violations/",
        f"/requests/violations/{v.pk}/",
        "/requests/violations/categories/",
        "/requests/violations/schedules/",
    ):
        assert client.get(url).status_code == 403
    assert client.post("/requests/violations/categories/", {"name": "X"}).status_code == 403


def test_board_flow(board_client, assoc):
    u = unit(assoc)
    r = board_client.post(
        "/requests/violations/categories/", {"name": "Noise", "rule_citation": "Rules §7"}
    )
    assert r.status_code == 302
    c = ViolationCategory.objects.get(name="Noise")
    board_client.post(
        "/requests/violations/schedules/",
        {"adopted_on": "2026-01-01", "effective_on": "2026-01-01"},
    )
    s = FineSchedule.objects.get()
    page = board_client.get("/requests/violations/schedules/").content.decode()
    assert "Not yet furnished to owners: fines cannot be imposed" in page
    board_client.post(
        f"/requests/violations/schedules/{s.pk}/",
        {"action": "line", "category": c.pk, "first_amount": "50", "repeat_amount": "100.00"},
    )
    assert s.lines.get().first_amount == Decimal("50.00")
    board_client.post(
        f"/requests/violations/schedules/{s.pk}/",
        {
            "action": "dates",
            "adopted_on": "2026-01-01",
            "effective_on": "2026-01-01",
            "furnished_on": "2026-01-10",
        },
    )
    s.refresh_from_db()
    assert s.furnished_on == D(2026, 1, 10)
    page = board_client.get("/requests/violations/schedules/").content.decode()
    assert "Not yet furnished" not in page

    r = board_client.post(
        "/requests/violations/add/",
        {
            "unit": u.pk,
            "category": c.pk,
            "observed_on": "2026-09-01",
            "description": "Loud party",
            "photos": [SimpleUploadedFile("p.jpg", PNG, content_type="image/jpeg")],
        },
    )
    v = Violation.objects.get()
    assert r.status_code == 302 and v.photos.count() == 1
    detail = board_client.get(f"/requests/violations/{v.pk}/").content.decode()
    assert "Rules §7" in detail and "$50.00" in detail
    board_client.post(f"/requests/violations/{v.pk}/status/", {"status": "dismissed"})
    v.refresh_from_db()
    assert v.status == "dismissed"
    assert board_client.get("/requests/violations/?status=open").context["violations"].count() == 0
    listing = board_client.get(f"/requests/violations/?unit={u.pk}").context["violations"]
    assert list(listing) == [v]


def test_new_version_copies_lines(board_client, assoc):
    s1 = schedule(assoc, 1, D(2025, 1, 1), furnished=D(2025, 1, 1))
    FineScheduleLine.objects.create(
        association=assoc,
        schedule=s1,
        category=category(assoc),
        first_amount=Decimal("25.00"),
        repeat_amount=Decimal("50.00"),
    )
    board_client.post(
        "/requests/violations/schedules/",
        {"adopted_on": "2026-01-01", "effective_on": "2026-02-01", "copy_lines": "on"},
    )
    s2 = FineSchedule.objects.get(version=2)
    assert s2.lines.get().first_amount == Decimal("25.00")
    assert s1.lines.count() == 1
