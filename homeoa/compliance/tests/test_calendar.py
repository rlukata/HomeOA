"""Compliance calendar, manual items, reminders and the community feed (GOV-10)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from icalendar import Calendar

from homeoa.access.testing import open_residents
from homeoa.community import services as community
from homeoa.compliance import calendar as cal
from homeoa.compliance.models import ManualItem, Recurrence
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos
from homeoa.jobs import periodic_jobs
from homeoa.notify.models import Notification
from homeoa.notify.services import board_recipients

User = get_user_model()
TODAY = dt.date(2026, 10, 7)
BOARD = "treasurer@cedarrow.example"
OWNER = "ownerc@cedarrow.example"


@pytest.fixture
def pilot(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


def _item(title="1099s", anchor=None, recurrence=Recurrence.YEARLY, **kw):
    return cal.save_item(
        ManualItem(
            title=title, anchor=anchor or TODAY + dt.timedelta(days=40), recurrence=recurrence, **kw
        )
    )


# --- recurrence ---------------------------------------------------------------------------


def test_monthly_recurrence_across_year_boundary(pilot):
    item = _item("Bank review", dt.date(2026, 11, 30), Recurrence.MONTHLY)
    days = item.due_dates(dt.date(2026, 11, 1), dt.date(2027, 3, 31))
    assert days == [
        dt.date(2026, 11, 30),
        dt.date(2026, 12, 30),
        dt.date(2027, 1, 30),
        dt.date(2027, 2, 28),
        dt.date(2027, 3, 30),
    ]


def test_mark_done_advances_across_year(pilot):
    item = _item("1099s", dt.date(2027, 1, 31))
    cal.mark_done(item, on=dt.date(2027, 1, 20))
    assert item.next_due == dt.date(2028, 1, 31) and item.active
    q = _item("Quarterly", dt.date(2026, 12, 15), Recurrence.QUARTERLY)
    cal.mark_done(q)
    assert q.next_due == dt.date(2027, 3, 15)
    once = _item("Annual report", dt.date(2026, 12, 1), Recurrence.ONCE)
    cal.mark_done(once)
    assert not once.active
    with pytest.raises(cal.CalendarError):
        cal.mark_done(once)


def test_suggestions_prefill_next_date():
    assert cal.suggestion("1099", TODAY)["anchor"] == dt.date(2027, 1, 31)
    assert cal.suggestion("1120h", dt.date(2026, 3, 1))["anchor"] == dt.date(2026, 4, 15)
    assert "anchor" not in cal.suggestion("annual_report", TODAY)
    assert cal.suggestion("nope", TODAY) == {}


# --- reminders ----------------------------------------------------------------------------


def _reminders(uid):
    return Notification.objects.filter(type_key=cal.REMINDER_TYPE, object_key=uid)


def test_reminders_once_per_offset_with_fixed_clock(pilot, settings):
    assert "compliance.reminders" in periodic_jobs()
    item = _item("Insurance renewal", TODAY + dt.timedelta(days=40))
    uid = cal.entries(pilot, item.next_due, item.next_due, vendors=False)[0].uid
    board = {p.pk for p in board_recipients(pilot)}
    assert board
    day = TODAY
    while day <= item.next_due + dt.timedelta(days=3):
        settings.HOMEOA_FIXED_TODAY = day.isoformat()
        cal.send_reminders(pilot, day)
        cal.send_reminders(pilot, day)  # a second run the same day sends nothing new
        day += dt.timedelta(days=1)
    notes = _reminders(uid)
    assert {n.period for n in notes} == {
        f"{item.next_due.isoformat()}:30",
        f"{item.next_due.isoformat()}:7",
    }
    assert notes.count() == 2 * len(board)
    assert {n.as_of for n in notes} == {
        item.next_due - dt.timedelta(days=30),
        item.next_due - dt.timedelta(days=7),
    }
    assert {n.recipient_id for n in notes} == board


def test_missed_days_send_only_nearest_offset(pilot):
    item = _item("Late", TODAY + dt.timedelta(days=5))
    cal.send_reminders(pilot, TODAY)
    uid = cal.entries(pilot, item.next_due, item.next_due, vendors=False)[0].uid
    assert {n.period for n in _reminders(uid)} == {f"{item.next_due.isoformat()}:7"}


def test_custom_offsets_and_done_items_skipped(pilot):
    cal.set_reminder_offsets(pilot, cal.parse_offsets("14, 3"))
    assert cal.reminder_offsets(pilot) == (14, 3)
    with pytest.raises(cal.CalendarError):
        cal.parse_offsets("0")
    with pytest.raises(cal.CalendarError):
        cal.parse_offsets("soon")
    item = _item("Soon", TODAY + dt.timedelta(days=14))
    uid = cal.entries(pilot, item.next_due, item.next_due, vendors=False)[0].uid
    cal.send_reminders(pilot, TODAY)
    assert {n.period for n in _reminders(uid)} == {f"{item.next_due.isoformat()}:14"}
    cal.mark_done(item)  # next year's occurrence is beyond the horizon
    cal.send_reminders(pilot, TODAY + dt.timedelta(days=11))
    assert not _reminders(uid).filter(period__endswith=":3").exists()


def test_board_only_type():
    from homeoa.notify import types as ntypes

    assert ntypes.get(cal.REMINDER_TYPE).audience == ntypes.AUDIENCE_BOARD


# --- community calendar / iCal --------------------------------------------------------------


def _summaries(client, email, pilot):
    token = community.feed_token(User.objects.get(email=email), pilot)
    resp = client.get(f"/community/ical/{token.token}.ics")
    assert resp.status_code == 200
    return {str(c.get("summary")) for c in Calendar.from_ical(resp.content).walk("VEVENT")}


def test_ical_board_only(client, pilot):
    _item("Master insurance renewal")
    board = _summaries(client, BOARD, pilot)
    assert any("Master insurance renewal" in s for s in board)
    owner = _summaries(client, OWNER, pilot)
    assert not any("Master insurance renewal" in s for s in owner)
    rows = community.occurrences(pilot, {"board"}, TODAY, TODAY + dt.timedelta(days=60))
    assert any(r.source == cal.SOURCE_KEY for r in rows)
    assert all(r.audience == "board" for r in rows if r.source == cal.SOURCE_KEY)


# --- overdue and page -------------------------------------------------------------------------


def test_overdue_item_is_red_todo(pilot):
    item = _item("State annual report", TODAY - dt.timedelta(days=2), Recurrence.ONCE)
    items = [t for t in todos.collect(pilot) if t.id == f"compliance-item-{item.pk}"]
    assert len(items) == 1 and items[0].kind == "enforce"
    cal.mark_done(item)
    assert not [t for t in todos.collect(pilot) if t.id == f"compliance-item-{item.pk}"]


def test_page_add_item_and_settings(client, pilot):
    client.force_login(User.objects.get(email=BOARD))
    _item("Overdue thing", TODAY - dt.timedelta(days=1), Recurrence.ONCE)
    resp = client.get("/governance/compliance/items/add/?suggest=1120h")
    assert resp.status_code == 200 and b"1120-H" in resp.content
    resp = client.post(
        "/governance/compliance/items/add/",
        {"title": "File 1120-H", "anchor": "2027-04-15", "recurrence": "yearly", "notes": ""},
    )
    assert resp.status_code == 302
    assert ManualItem.objects.get(title="File 1120-H").next_due == dt.date(2027, 4, 15)
    client.post("/governance/compliance/reminders/", {"days": "21, 3"})
    pilot.refresh_from_db()
    assert cal.reminder_offsets(pilot) == (21, 3)
    page = client.get("/governance/compliance/").content.decode()
    assert "File 1120-H" in page and "Overdue thing" in page and "badge danger" in page


def test_owner_cannot_manage(client, pilot):
    client.force_login(User.objects.get(email=OWNER))
    resp = client.post("/governance/compliance/reminders/", {"days": "1"})
    assert resp.status_code in (302, 403)
    pilot.refresh_from_db()
    assert cal.reminder_offsets(pilot) == cal.DEFAULT_OFFSETS


def test_mark_done_with_attachment_files_proof(pilot):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from homeoa.documents.models import Visibility

    item = _item("Insurance renewal", dt.date(2026, 11, 1))
    up = SimpleUploadedFile("binder.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    cal.mark_done(item, upload=up)
    item.refresh_from_db()
    doc = item.last_done_document
    assert doc is not None and doc.visibility == Visibility.BOARD
    assert doc.current_version.file_name == "binder.pdf"
    bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
    with pytest.raises(cal.CalendarError):
        cal.mark_done(item, upload=bad)


def test_editing_anchor_recomputes_next_due(pilot):
    item = _item("Quarterly", dt.date(2026, 10, 15), Recurrence.QUARTERLY)
    cal.mark_done(item, on=dt.date(2026, 10, 10))
    assert item.next_due == dt.date(2027, 1, 15)
    item.anchor = dt.date(2026, 10, 20)
    cal.save_item(item)
    assert item.next_due == dt.date(2027, 1, 20)
    item.anchor = dt.date(2027, 6, 1)
    cal.save_item(item)
    assert item.next_due == dt.date(2027, 6, 1)
