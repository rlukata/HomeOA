"""Meetings, agenda and statutory notice (GOV-03)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa import rules
from homeoa.access import services as access
from homeoa.access.testing import open_residents
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.governance import services as gov
from homeoa.letters.models import MailBatch
from homeoa.meetings import calendar, services
from homeoa.meetings.models import Meeting, MeetingNotice
from homeoa.notify import commlog
from homeoa.notify import services as nsvc
from homeoa.notify.models import Delivery, Notification
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Unit
from homeoa.rules.base import Basis, RulePack, RuleValue
from homeoa.rules.keys import KEYS

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


def _go_live(a):
    open_residents(a)
    a.refresh_from_db()
    services.set_owner_notices_live(a, True)


def _consent(number):
    owner = reg.current_owners(Unit.objects.get(number=number))[0]
    owner.email = owner.email or f"owner{number.lower()}@example.com"
    owner.save()
    reg.record_notice_preference(
        owner, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )
    return owner


def _meeting(a, kind=Meeting.Kind.ANNUAL, date=NOV20):
    return services.create_meeting(a, kind=kind, date=date, title="Annual meeting")


def _units_with_owners(a):
    return [u for u in Unit.objects.all() if reg.current_owners(u, TODAY)]


# --- window -----------------------------------------------------------------------------


def test_window_from_active_pack_values(pilot):
    m = _meeting(pilot)
    w = services.notice_window(m, TODAY)
    expected_min = rules.value(pilot, "notices.owner_meeting.min_days")
    expected_max = rules.value(pilot, "notices.owner_meeting.max_days")
    assert expected_min is not None and expected_max is not None
    assert w.latest == m.date - dt.timedelta(days=expected_min)
    assert w.earliest == m.date - dt.timedelta(days=expected_max)


def test_generic_pack_has_placeholder_defaults():
    from homeoa.rules.packs.generic import pack

    for kind in ("owner_meeting", "special_meeting", "board_meeting"):
        for end in ("min_days", "max_days"):
            if kind == "board_meeting" and end == "max_days":
                assert f"notices.{kind}.{end}" not in KEYS  # No earliest date
                continue
            rv = pack.get(f"notices.{kind}.{end}")
            assert rv.value is not None and rv.basis == Basis.POLICY and not rv.verified


def test_window_from_board_meeting_keys(pilot, monkeypatch):
    values = {k: RuleValue(value=None, basis=Basis.BOARD) for k in KEYS}
    values["notices.board_meeting.min_days"] = RuleValue(
        value=5, basis=Basis.STATUTE, citation="Test 1.2"
    )
    pack = RulePack(id="test", name="Test", jurisdiction="XX", values=values)
    monkeypatch.setattr(rules, "active_pack", lambda association: pack)
    m = _meeting(pilot, kind=Meeting.Kind.BOARD, date=D(2026, 10, 20))
    w = services.notice_window(m, TODAY)
    assert (w.earliest, w.latest) == (None, D(2026, 10, 15))
    assert w.max_rule is None
    assert w.min_rule.citation == "Test 1.2"
    assert services.window_problem(m, TODAY) == ""  # no earliest date for board meetings
    assert services.window_problem(m, D(2026, 10, 12)) == ""
    assert "Too late" in services.window_problem(m, D(2026, 10, 16))


def test_send_outside_window_blocked(pilot):
    _go_live(pilot)
    too_far = _meeting(pilot, date=TODAY + dt.timedelta(days=200))
    with pytest.raises(services.MeetingError, match="Too early"):
        services.send_notice(too_far, services.MODE_OWNERS)
    too_close = _meeting(pilot, date=TODAY + dt.timedelta(days=1))
    with pytest.raises(services.MeetingError, match="Too late"):
        services.send_notice(too_close, services.MODE_PRINT)
    assert not MeetingNotice.objects.exists()


# --- to-do ------------------------------------------------------------------------------


def test_todo_due_on_last_allowed_day(pilot):
    from homeoa.meetings.todos import todo_items

    m = _meeting(pilot)
    board = User.objects.get(email="secretary@cedarrow.example")
    shown = [t for t in todos.collect(pilot, board) if t.source == "meetings"]
    assert len(shown) == 1
    latest = services.notice_window(m, TODAY).latest
    raw = todo_items(pilot)
    assert len(raw) == 1 and raw[0]["due"] == latest
    assert "Send notice by" in raw[0]["title"]
    m.status = Meeting.Status.NOTICED
    m.save()
    assert todo_items(pilot) == []


# --- sending ----------------------------------------------------------------------------


def test_owners_mode_blocked_before_cutover(pilot):
    m = _meeting(pilot)
    with pytest.raises(services.MeetingError, match="not switched on"):
        services.send_notice(m, services.MODE_OWNERS)
    open_residents(pilot)
    with pytest.raises(services.MeetingError, match="not switched on"):
        services.send_notice(m, services.MODE_OWNERS)  # owner_notices_live still off


def test_email_vs_letter_split_and_commlog(pilot, django_capture_on_commit_callbacks):
    _go_live(pilot)
    owner_a = _consent("A")
    m = _meeting(pilot)
    services.add_agenda_item(m, title="Ratify the 2027 budget")
    with django_capture_on_commit_callbacks(execute=True):
        result = services.send_notice(m, services.MODE_OWNERS)
    units = _units_with_owners(pilot)
    assert result.email >= 1 and result.mail >= 1
    assert result.email + result.mail == len(units)
    m.refresh_from_db()
    assert m.status == Meeting.Status.NOTICED and m.noticed_on == TODAY

    unit_a = Unit.objects.get(number="A")
    a_notice = MeetingNotice.objects.get(meeting=m, unit=unit_a)
    assert a_notice.channel == MeetingNotice.Channel.EMAIL
    assert "Ratify the 2027 budget" in a_notice.body
    log = list(commlog.entries(unit_a))
    assert any(d.channel == "email" and d.notification.recipient == owner_a for d in log)

    unit_b = next(u for u in units if not all(nsvc.email_allowed(p) for p in reg.current_owners(u)))
    b_notice = MeetingNotice.objects.get(meeting=m, unit=unit_b)
    assert b_notice.channel == MeetingNotice.Channel.MAIL and b_notice.letter is not None
    assert b_notice.batch.status != MailBatch.Status.DRAFT
    assert any(d.channel == "postal" for d in commlog.entries(unit_b))


def test_resend_is_idempotent(pilot, django_capture_on_commit_callbacks):
    _go_live(pilot)
    _consent("A")
    m = _meeting(pilot)
    with django_capture_on_commit_callbacks(execute=True):
        services.send_notice(m, services.MODE_OWNERS)
    count = MeetingNotice.objects.count()
    notes = Notification.objects.count()
    with django_capture_on_commit_callbacks(execute=True):
        again = services.send_notice(m, services.MODE_OWNERS)
    assert again.total == 0 and again.skipped == len(Unit.objects.all())
    assert MeetingNotice.objects.count() == count and Notification.objects.count() == notes


def test_print_only_before_cutover(pilot, django_capture_on_commit_callbacks):
    m = _meeting(pilot)
    with django_capture_on_commit_callbacks(execute=True):
        result = services.send_notice(m, services.MODE_PRINT)
    assert result.printed == len(_units_with_owners(pilot)) and result.email == 0
    batch = result.batch
    assert batch.status == MailBatch.Status.DRAFT
    assert not Delivery.objects.exclude(status=Delivery.Status.PREVIEW).exists()
    assert set(MeetingNotice.objects.values_list("channel", flat=True)) == {"print"}


def test_board_copy_before_cutover(pilot, django_capture_on_commit_callbacks):
    m = _meeting(pilot)
    with django_capture_on_commit_callbacks(execute=True):
        result = services.send_notice(m, services.MODE_BOARD)
    assert result.board >= 1
    m.refresh_from_db()
    assert m.status == Meeting.Status.DRAFT  # a board copy is not the statutory notice
    for d in Delivery.objects.all():
        assert d.channel == "email"
        assert d.notification.recipient.user is not None
        assert access.is_board_member(d.notification.recipient.user, pilot)
    with django_capture_on_commit_callbacks(execute=True):
        assert services.send_notice(m, services.MODE_BOARD).board == 0


def test_cancelled_meeting_gets_no_notice(pilot):
    m = _meeting(pilot)
    services.set_status(m, Meeting.Status.CANCELLED)
    with pytest.raises(services.MeetingError):
        services.send_notice(m, services.MODE_PRINT)


# --- agenda, resolution link, calendar --------------------------------------------------


def test_agenda_order_and_resolution_link(pilot):
    m = _meeting(pilot)
    res = gov.create_resolution(pilot, title="Adopt reserve study")
    first = services.add_agenda_item(m, title="Call to order")
    second = services.add_agenda_item(m, title="Reserve study", resolution=res)
    services.move_agenda_item(second, -1)
    assert [i.pk for i in m.agenda.all()] == [second.pk, first.pk]
    res.meeting = m
    res.save()
    assert list(m.resolutions.all()) == [res]
    assert "1. Reserve study" in services.agenda_text(m)


def test_calendar_events_audience(pilot):
    m = _meeting(pilot)
    ev = calendar.events(pilot, D(2026, 11, 1), D(2026, 11, 30))
    assert ev[0].audience == "board"
    m.status = Meeting.Status.NOTICED
    m.save()
    assert calendar.events(pilot, D(2026, 11, 1), D(2026, 11, 30))[0].audience == "all"


def test_meeting_in_community_calendar(pilot):
    from homeoa.community import services as community

    assert "meetings" in {s.key for s in community.sources()}
    m = _meeting(pilot)
    start, end = D(2026, 11, 1), D(2026, 11, 30)
    board = community.occurrences(pilot, ["all", "board"], start, end)
    assert [o.uid for o in board if o.source == "meetings"] == [f"meeting-{m.pk}@homeoa"]
    assert not [
        o
        for o in community.occurrences(pilot, ["all", "owners"], start, end)
        if o.source == "meetings"
    ]  # draft: board only
    m.status = Meeting.Status.NOTICED
    m.save()
    owners = community.occurrences(pilot, ["all", "owners"], start, end)
    assert [o.title for o in owners if o.source == "meetings"] == [m.display_title]


def test_isolation(pilot):
    _meeting(pilot)
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        assert Meeting.objects.count() == 0
        assert calendar.events(other, D(2026, 1, 1), D(2026, 12, 31)) == []


# --- views ------------------------------------------------------------------------------


def test_views(client, pilot):
    client.force_login(User.objects.get(email="secretary@cedarrow.example"))
    assert client.get("/governance/meetings/").status_code == 200
    resp = client.post(
        "/governance/meetings/add/",
        {"kind": "board", "date": "2026-10-20", "start_time": "19:00", "location": "Clubhouse"},
    )
    m = Meeting.objects.get(kind="board")
    assert resp.status_code == 302 and resp["Location"] == f"/governance/meetings/{m.pk}/"
    client.post(f"/governance/meetings/{m.pk}/agenda/add/", {"title": "Approve minutes"})
    page = client.get(f"/governance/meetings/{m.pk}/").content.decode()
    assert "Approve minutes" in page and "Verified" in page  # the RCW 64.90.445 notice rule
    resp = client.post(f"/governance/meetings/{m.pk}/notice/", {"mode": "owners"})
    assert MeetingNotice.objects.count() == 0  # blocked before cutover

    client.logout()
    open_residents(pilot)
    client.force_login(User.objects.get(email="ownerc@cedarrow.example"))
    assert client.get("/governance/meetings/").status_code == 403
    assert client.post("/governance/meetings/add/", {"kind": "board"}).status_code == 403
