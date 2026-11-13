"""Minutes, attendance, quorum and proxies (GOV-04, GOV-05)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import open_residents
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents import services as docs
from homeoa.documents.models import Visibility
from homeoa.esign.models import Envelope
from homeoa.governance.models import BoardTerm
from homeoa.meetings import minutes as msvc
from homeoa.meetings import quorum, services
from homeoa.meetings.models import Attendance, Meeting, Minutes
from homeoa.meetings.services import MeetingError
from homeoa.plugins import registry
from homeoa.registry.models import Occupancy, Person, Unit

D = dt.date
TODAY = D(2026, 10, 7)
NOV20 = D(2026, 11, 20)
User = get_user_model()


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def _owners_meeting(a, date=NOV20):
    return services.create_meeting(a, kind=Meeting.Kind.ANNUAL, date=date, title="Annual")


def _unit(n):
    return Unit.objects.get(number=n)


def _set_weighting(a, mode):
    a.settings = {**(a.settings or {}), "vote_weighting": mode}
    a.save(update_fields=["settings"])


# --- quorum -----------------------------------------------------------------------------


def test_quorum_per_unit(pilot):
    _set_weighting(pilot, "per_unit")
    quorum.set_requirement(pilot, Meeting.Kind.ANNUAL, "percent", "50")
    m = _owners_meeting(pilot)
    for n in "ABCD":
        msvc.set_attendance(m, mode="in_person", unit=_unit(n))
    q = quorum.compute(m)
    assert q.total_count == 9 and q.present_count == 4
    assert q.present_weight == Decimal("4") and not q.met
    msvc.set_attendance(m, mode="remote", unit=_unit("E"))
    q = quorum.compute(m)
    assert q.met and q.remote == 1 and q.in_person == 4


def test_quorum_weighted_by_allocated_interest(pilot):
    _set_weighting(pilot, "allocated_interest")
    quorum.set_requirement(pilot, Meeting.Kind.ANNUAL, "percent", "50")
    m = _owners_meeting(pilot)
    units = sorted(Unit.objects.all(), key=lambda u: -u.allocated_interest)
    total = sum((u.allocated_interest for u in units), Decimal("0"))
    present = Decimal("0")
    for u in units:
        if present * 2 >= total:
            break
        msvc.set_attendance(m, mode="in_person", unit=u)
        present += u.allocated_interest
    q = quorum.compute(m)
    assert isinstance(q.present_weight, Decimal)
    assert q.present_weight == present and q.total_weight == total
    assert q.met


def test_quorum_count_basis(pilot):
    quorum.set_requirement(pilot, Meeting.Kind.ANNUAL, "count", "3")
    m = _owners_meeting(pilot)
    msvc.set_attendance(m, mode="in_person", unit=_unit("A"))
    msvc.set_attendance(m, mode="in_person", unit=_unit("B"))
    assert not quorum.compute(m).met
    msvc.add_proxy(m, unit=_unit("C"), holder="Board president", granted_on=TODAY)
    assert quorum.compute(m).met


def test_proxy_counts_once_and_revocation_removes_it(pilot):
    _set_weighting(pilot, "per_unit")
    m = _owners_meeting(pilot)
    p = msvc.add_proxy(m, unit=_unit("A"), holder="Jane", granted_on=TODAY)
    with pytest.raises(MeetingError):
        msvc.add_proxy(m, unit=_unit("A"), holder="Joe", granted_on=TODAY)
    q = quorum.compute(m)
    assert q.present_count == 1 and q.by_proxy == 1
    msvc.revoke_proxy(p, on=TODAY)
    q = quorum.compute(m)
    assert q.present_count == 0 and q.by_proxy == 0
    # a fresh proxy may be given after revoking
    msvc.add_proxy(m, unit=_unit("A"), holder="Joe", granted_on=TODAY)
    assert quorum.compute(m).by_proxy == 1


def test_proxy_for_unit_present_is_ignored(pilot):
    m = _owners_meeting(pilot)
    msvc.add_proxy(m, unit=_unit("A"), holder="Jane", granted_on=TODAY)
    msvc.set_attendance(m, mode="in_person", unit=_unit("A"))
    q = quorum.compute(m)
    assert q.present_count == 1 and q.by_proxy == 0
    assert [p.unit.number for p in q.ignored_proxies] == ["A"]


def test_proxy_revocable_only_before_meeting(pilot):
    m = _owners_meeting(pilot, date=TODAY - dt.timedelta(days=1))
    p = msvc.add_proxy(m, unit=_unit("A"), holder="Jane", granted_on=m.date)
    with pytest.raises(MeetingError):
        msvc.revoke_proxy(p, on=TODAY)


def test_proxy_upload_filed_board_only(pilot):
    m = _owners_meeting(pilot)
    p = msvc.add_proxy(
        m, unit=_unit("B"), holder="Jane", granted_on=TODAY, upload=("proxy.pdf", b"%PDF-1.4 x")
    )
    assert p.document.document.visibility == Visibility.BOARD


def test_no_quorum_setting_means_none_unless_pack(pilot, monkeypatch):
    m = _owners_meeting(pilot)
    req = quorum.requirement(m)
    assert req.source in ("pack", "none")
    board = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=NOV20)
    assert quorum.requirement(board).source == "none"
    assert quorum.compute(board).met


def test_board_meeting_quorum_counts_members(pilot):
    # The demo seed puts a president, vice president, secretary and treasurer on the board.
    board = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=NOV20)
    members = quorum.board_members(board)
    assert len(members) == 4
    quorum.set_requirement(pilot, Meeting.Kind.BOARD, "count", str(len(members)))
    for p in members[:-1]:
        msvc.set_attendance(board, mode="remote", person=p)
    assert not quorum.compute(board).met
    msvc.set_attendance(board, mode="in_person", person=members[-1])
    assert quorum.compute(board).met
    with pytest.raises(MeetingError):
        msvc.add_proxy(board, unit=_unit("A"), holder="x", granted_on=TODAY)


def test_set_requirement_rejects_bad_percent(pilot):
    with pytest.raises(ValueError):
        quorum.set_requirement(pilot, Meeting.Kind.ANNUAL, "percent", "150")
    quorum.set_requirement(pilot, Meeting.Kind.ANNUAL, "", "")
    assert quorum.setting_for(pilot, Meeting.Kind.ANNUAL) == ("", None)


def test_clear_attendance(pilot):
    m = _owners_meeting(pilot)
    msvc.set_attendance(m, mode="in_person", unit=_unit("A"))
    msvc.set_attendance(m, mode="", unit=_unit("A"))
    assert not Attendance.objects.filter(meeting=m).exists()


# --- minutes ----------------------------------------------------------------------------


def _approved(a):
    held = services.create_meeting(a, kind=Meeting.Kind.BOARD, date=D(2026, 9, 1))
    nxt = services.create_meeting(a, kind=Meeting.Kind.BOARD, date=D(2026, 10, 1))
    mins = msvc.get_or_create(held)
    msvc.save_draft(mins, "Called to order.\n\nAdjourned.")
    msvc.approve(mins, at_meeting=nxt, on=D(2026, 10, 1))
    return mins, nxt


def test_minutes_state_machine_and_immutability(pilot):
    mins, nxt = _approved(pilot)
    assert mins.status == Minutes.Status.APPROVED
    assert mins.approved_at == nxt and mins.approved_on == D(2026, 10, 1)
    with pytest.raises(MeetingError):
        msvc.save_draft(mins, "changed")
    mins.refresh_from_db()
    mins.body = "tampered"
    with pytest.raises(ValueError):
        mins.save()
    mins.refresh_from_db()
    mins.status = Minutes.Status.DRAFT
    with pytest.raises(ValueError):
        mins.save()
    mins.refresh_from_db()
    with pytest.raises(MeetingError):
        msvc.approve(mins, at_meeting=nxt, on=D(2026, 10, 1))


def test_approve_needs_later_meeting(pilot):
    held = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=D(2026, 9, 1))
    mins = msvc.get_or_create(held)
    with pytest.raises(MeetingError):
        msvc.approve(mins, at_meeting=held, on=D(2026, 9, 1))  # empty and same meeting
    msvc.save_draft(mins, "text")
    with pytest.raises(MeetingError):
        msvc.approve(mins, at_meeting=held, on=D(2026, 9, 1))


def test_publish_needs_approval(pilot):
    held = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=D(2026, 9, 1))
    mins = msvc.get_or_create(held)
    msvc.save_draft(mins, "text")
    with pytest.raises(MeetingError):
        msvc.publish(mins)


def _tenant_user(a):
    usr = User.objects.create_user("tenant-m405@example.org")
    person = Person.objects.create(full_name="T", email="tenant-m405@example.org", user=usr)
    Occupancy.objects.create(
        unit=_unit("E"), person=person, kind=Occupancy.Kind.TENANT, start=D(2024, 1, 1)
    )
    return usr


def test_publish_visible_to_owners_not_tenants(pilot):
    open_residents(pilot)
    pilot.refresh_from_db()
    mins, _ = _approved(pilot)
    msvc.publish(mins)
    mins.refresh_from_db()
    assert mins.status == Minutes.Status.PUBLISHED
    doc = mins.document
    assert doc.category == "minutes" and doc.visibility == Visibility.OWNERS
    assert doc.current_version.content_type == "application/pdf"
    assert docs.read_file(doc.current_version).startswith(b"%PDF")
    owner = User.objects.get(email="ownerc@cedarrow.example")
    tenant = _tenant_user(pilot)
    assert docs.can_view(owner, pilot, doc.visibility)
    assert not docs.can_view(tenant, pilot, doc.visibility)


def test_publish_to_tenants_when_chosen(pilot):
    mins, _ = _approved(pilot)
    msvc.publish(mins, visibility=Visibility.TENANTS)
    assert mins.document.visibility == Visibility.TENANTS
    with pytest.raises(MeetingError):
        msvc.publish(mins)  # already published


def test_send_for_signature_to_secretary(pilot, django_capture_on_commit_callbacks):
    mins, _ = _approved(pilot)
    with pytest.raises(MeetingError):
        msvc.send_for_signature(mins)  # not yet published
    msvc.publish(mins)
    sec = msvc.secretary(pilot, TODAY)
    if sec is None:
        sec = Person.objects.create(full_name="Sam Secretary", email="sec@example.org")
        BoardTerm.objects.create(person=sec, office="secretary", start=D(2020, 1, 1))
    with django_capture_on_commit_callbacks(execute=True):
        env = msvc.send_for_signature(mins)
    assert env.status == Envelope.Status.SENT
    assert env.signers.get().email == sec.email
    with pytest.raises(MeetingError):
        msvc.send_for_signature(mins)


def test_minutes_and_attendance_changes_are_audited(pilot):
    from homeoa.audit.models import AuditEvent

    m = _owners_meeting(pilot)
    mins = msvc.get_or_create(m)
    before = AuditEvent.objects.filter(object_type__icontains="minutes").count()
    msvc.save_draft(mins, "Called to order.")
    events = AuditEvent.objects.filter(object_type__icontains="minutes", object_id=str(mins.pk))
    assert events.count() > before

    def att():
        return AuditEvent.objects.filter(object_type__icontains="attendance").count()

    n0 = att()
    msvc.set_attendance(m, mode="in_person", unit=_unit("A"))
    n1 = att()
    msvc.set_attendance(m, mode="remote", unit=_unit("A"))
    n2 = att()
    msvc.set_attendance(m, mode="", unit=_unit("A"))
    assert n0 < n1 < n2 < att()


def test_signed_copy_filed_as_new_version(pilot, django_capture_on_commit_callbacks):
    from homeoa.esign import services as esign

    mins, _ = _approved(pilot)
    msvc.publish(mins)
    sec = Person.objects.create(full_name="Sam Secretary", email="sec@example.org")
    BoardTerm.objects.filter(office="secretary").delete()
    BoardTerm.objects.create(person=sec, office="secretary", start=D(2020, 1, 1))
    with django_capture_on_commit_callbacks(execute=True):
        env = msvc.send_for_signature(mins)
    token = esign.issue_token(env.signers.get())
    with django_capture_on_commit_callbacks(execute=True):
        esign.sign(token, typed_name="Sam Secretary", consent=True)
    mins.document.refresh_from_db()
    assert mins.document.current_version.number == 2


# --- views ------------------------------------------------------------------------------


def test_minutes_and_attendance_views(client, pilot):
    client.force_login(User.objects.get(email="secretary@cedarrow.example"))
    held = services.create_meeting(pilot, kind=Meeting.Kind.ANNUAL, date=D(2026, 9, 1))
    nxt = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=D(2026, 10, 1))
    base = f"/governance/meetings/{held.pk}"
    assert client.get(f"{base}/").status_code == 200
    assert client.get(f"{base}/minutes/").status_code == 200
    client.post(f"{base}/minutes/save/", {"body": "Called to order."})
    assert "Called to order." in client.get(f"{base}/minutes/").content.decode()
    client.post(f"{base}/minutes/approve/", {"approved_at": nxt.pk, "approved_on": "2026-10-01"})
    client.post(f"{base}/minutes/publish/", {"visibility": "owners"})
    mins = Minutes.objects.get(meeting=held)
    assert mins.status == Minutes.Status.PUBLISHED
    assert "Published PDF" in client.get(f"{base}/minutes/").content.decode()

    assert client.get(f"{base}/attendance/").status_code == 200
    client.post(f"{base}/quorum/", {"basis": "count", "value": "1"})
    resp = client.post(
        f"{base}/attendance/set/",
        {"who": _unit("A").pk, "mode": "in_person"},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200 and "Quorum met" in resp.content.decode()
    client.post(
        f"{base}/proxies/add/",
        {"unit": _unit("B").pk, "holder": "Jane", "granted_on": "2026-08-30"},
    )
    assert quorum.compute(held).by_proxy == 1

    client.logout()
    open_residents(pilot)
    client.force_login(User.objects.get(email="ownerc@cedarrow.example"))
    assert client.post(f"{base}/minutes/save/", {"body": "x"}).status_code == 403
    assert client.post(f"{base}/attendance/set/", {"who": 1, "mode": "remote"}).status_code == 403


def test_draft_minutes_home_todo_until_approved(pilot):
    from homeoa.dashboard import todos

    past = _owners_meeting(pilot, date=D(2026, 9, 1))
    record = msvc.get_or_create(past)
    tid = f"meetings-minutes-{past.pk}"
    items = [t for t in todos.collect(pilot) if t.id == tid]
    assert len(items) == 1 and items[0].title.startswith("Approve minutes of")
    future = _owners_meeting(pilot)
    msvc.get_or_create(future)  # a draft for a future meeting is not a to-do
    assert not [t for t in todos.collect(pilot) if t.id == f"meetings-minutes-{future.pk}"]
    msvc.save_draft(record, "Called to order.")
    msvc.approve(record, at_meeting=future, on=NOV20)
    assert not [t for t in todos.collect(pilot) if t.id == tid]
