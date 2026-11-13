"""Announcements by audience, community calendar and iCal feed (COM-01, COM-06)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from icalendar import Calendar

from homeoa.access import services as access
from homeoa.access.testing import close_residents, open_residents
from homeoa.community import services
from homeoa.community.models import Announcement, Audience, CalendarEvent, CalendarFeedToken
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.notify.models import Notification

User = get_user_model()
TODAY = dt.date(2026, 10, 7)
BOARD = "treasurer@cedarrow.example"
OWNER = "ownerc@cedarrow.example"
TENANT = "chris.doyle@cedarrow.example"


@pytest.fixture
def pilot(db, settings):
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


def _u(email):
    return User.objects.get(email=email)


def _ann(audience, title=None, **kw):
    kw.setdefault("publish_on", TODAY)
    return services.save_announcement(
        Announcement(title=title or f"For {audience}", body="Hello", audience=audience, **kw)
    )


def _event(audience, title=None, **kw):
    kw.setdefault("start_date", TODAY + dt.timedelta(days=3))
    return CalendarEvent.objects.create(title=title or f"Event {audience}", audience=audience, **kw)


# --- audiences --------------------------------------------------------------------------

MATRIX = {
    BOARD: {"all", "owners", "tenants", "board"},
    OWNER: {"all", "owners"},
    TENANT: {"all", "tenants"},
}


@pytest.mark.parametrize("email", list(MATRIX))
def test_audience_matrix(pilot, email):
    for aud in Audience.values:
        _ann(aud)
        _event(aud)
    audiences = services.audiences_for(_u(email), pilot)
    assert set(audiences) == MATRIX[email]
    seen = {a.audience for a in services.visible_announcements(pilot, audiences)}
    assert seen == MATRIX[email]
    events = {o.audience for o in services.upcoming(pilot, audiences)}
    assert events == MATRIX[email]


def test_residents_off_sees_nothing(pilot):
    close_residents(pilot)
    access.clear_cache(_u(TENANT))
    assert services.audiences_for(_u(TENANT), pilot) == frozenset()


def test_tenant_home_shows_only_allowed(client, pilot):
    _ann("all", "Gutter day")
    _ann("owners", "Owner only budget")
    _ann("tenants", "Tenant welcome")
    _ann("board", "Board secret")
    client.force_login(_u(TENANT))
    html = client.get("/me/").content.decode()
    assert "Gutter day" in html and "Tenant welcome" in html
    assert "Owner only budget" not in html and "Board secret" not in html
    page = client.get("/community/me/").content.decode()
    assert "Tenant welcome" in page and "Board secret" not in page


# --- scheduling ---------------------------------------------------------------------------


def test_scheduled_and_expired(pilot, settings):
    _ann("all", "Future", publish_on=TODAY + dt.timedelta(days=2))
    _ann("all", "Old", publish_on=TODAY - dt.timedelta(days=9), expires_on=TODAY - dt.timedelta(1))
    _ann("all", "Last day", publish_on=TODAY - dt.timedelta(days=3), expires_on=TODAY)
    titles = lambda: {a.title for a in services.visible_announcements(pilot, {"all"})}  # noqa: E731
    assert titles() == {"Last day"}
    settings.HOMEOA_FIXED_TODAY = (TODAY + dt.timedelta(days=2)).isoformat()
    assert titles() == {"Future"}


def test_expiry_before_publish_rejected(pilot):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _ann("all", publish_on=TODAY, expires_on=TODAY - dt.timedelta(days=1))


# --- notify ------------------------------------------------------------------------------


def test_notify_once_on_commit(pilot, monkeypatch):
    calls = []
    from homeoa.notify import services as nsvc

    real = nsvc.notify
    monkeypatch.setattr(nsvc, "notify", lambda *a, **k: calls.append(a) or real(*a, **k))
    ann = _ann("owners", "Meeting", also_notify=True)
    assert len(calls) == 1
    assert ann.notified_on == TODAY
    services.save_announcement(ann)  # an edit does not resend
    assert services.send_due(pilot, TODAY) == 0
    assert len(calls) == 1
    notes = Notification.objects.filter(type_key=services.NOTIFY_TYPE)
    recipients = {n.recipient.user.email for n in notes if n.recipient.user}
    assert OWNER in recipients and TENANT not in recipients
    # Never by letter: only in-app, plus email where consent allows.
    channels = {d.channel for n in notes for d in n.deliveries.all()}
    assert channels <= {"inapp", "email"}


@pytest.mark.deferred_notify
def test_notify_deferred_until_commit(pilot, monkeypatch):
    from django.db import transaction

    from homeoa.notify import services as nsvc

    queued = []
    monkeypatch.setattr(nsvc, "_schedule", queued.append)
    calls = []
    monkeypatch.setattr(nsvc, "notify", lambda *a, **k: calls.append(a) or [])
    with transaction.atomic():
        _ann("all", also_notify=True)
        assert calls == []
    assert len(queued) == 1
    queued[0]()
    assert len(calls) == 1


def test_scheduled_notify_sent_by_job(pilot, settings):
    ann = _ann("all", "Later", publish_on=TODAY + dt.timedelta(days=1), also_notify=True)
    assert ann.notified_on is None
    assert not Notification.objects.filter(type_key=services.NOTIFY_TYPE).exists()
    assert services.send_due(pilot, TODAY + dt.timedelta(days=1)) == 1
    assert services.send_due(pilot, TODAY + dt.timedelta(days=1)) == 0
    assert Notification.objects.filter(type_key=services.NOTIFY_TYPE).exists()


# --- iCal ---------------------------------------------------------------------------------


def _parse(resp):
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/calendar")
    return Calendar.from_ical(resp.content)


def test_ics_valid_and_filtered(client, pilot):
    timed = _event("all", "Board meeting", start_time=dt.time(19), end_time=dt.time(20, 30))
    _event("tenants", "Tenant mixer", end_date=TODAY + dt.timedelta(days=4))
    _event("owners", "Owner budget session")
    _event("board", "Exec session")
    token = services.feed_token(_u(TENANT), pilot)
    client.logout()
    cal = _parse(client.get(f"/community/ical/{token.token}.ics"))
    events = {str(e["summary"]): e for e in cal.walk("VEVENT")}
    assert set(events) == {"Board meeting", "Tenant mixer"}
    ev = events["Board meeting"]
    assert str(ev["uid"]) == services.event_uid(timed)
    assert ev.get("dtstamp") is not None
    assert ev["dtstart"].params["TZID"] == pilot.timezone
    assert ev["dtstart"].dt.hour == 19
    zones = [str(z["tzid"]) for z in cal.walk("VTIMEZONE")]
    assert zones == [pilot.timezone]
    mixer = events["Tenant mixer"]
    assert mixer["dtstart"].dt == TODAY + dt.timedelta(days=3)
    assert mixer["dtend"].dt == TODAY + dt.timedelta(days=5)
    # UID stable across fetches.
    again = _parse(client.get(f"/community/ical/{token.token}.ics"))
    assert {str(e["uid"]) for e in again.walk("VEVENT")} == {
        str(e["uid"]) for e in cal.walk("VEVENT")
    }


def test_token_revoke_and_reset(client, pilot):
    client.force_login(_u(OWNER))
    client.post("/community/feed/reset/")
    first = CalendarFeedToken.objects.get(user=_u(OWNER), revoked_at__isnull=True).token
    assert client.get(f"/community/ical/{first}.ics").status_code == 200
    client.post("/community/feed/reset/")
    second = CalendarFeedToken.objects.get(user=_u(OWNER), revoked_at__isnull=True).token
    assert second != first
    assert client.get(f"/community/ical/{first}.ics").status_code == 404
    client.post("/community/feed/revoke/")
    assert client.get(f"/community/ical/{second}.ics").status_code == 404
    assert client.get("/community/ical/nonsense.ics").status_code == 404


def test_feed_404_when_residents_off(client, pilot):
    token = services.feed_token(_u(TENANT), pilot)
    close_residents(pilot)
    assert client.get(f"/community/ical/{token.token}.ics").status_code == 404


# --- sources ------------------------------------------------------------------------------


def test_registered_source(pilot):
    def meetings(association, start, end):
        return [services.Occurrence(uid="mtg-1@homeoa", title="Annual meeting", start_date=end)]

    services.register_source("test-source", "Test", meetings)
    try:
        rows = services.upcoming(pilot, {"all"}, TODAY, days=10)
        assert [r.title for r in rows] == ["Annual meeting"]
        ics = services.build_ics(pilot, rows).decode()
        assert "UID:mtg-1@homeoa" in ics
    finally:
        services.unregister_source("test-source")


# --- board pages and permissions -------------------------------------------------------------


def test_board_creates_announcement_and_event(client, pilot):
    client.force_login(_u(BOARD))
    resp = client.post(
        "/community/announcements/new/",
        {
            "title": "Gutter cleaning",
            "body": "Move cars",
            "audience": "all",
            "publish_on": TODAY.isoformat(),
        },
    )
    assert resp.status_code == 302
    assert "Gutter cleaning" in client.get("/community/announcements/").content.decode()
    resp = client.post(
        "/community/calendar/new/",
        {
            "title": "Board meeting",
            "start_date": "2026-10-15",
            "start_time": "19:00",
            "end_time": "20:00",
            "audience": "all",
        },
    )
    assert resp.status_code == 302
    assert "Board meeting" in client.get("/community/calendar/").content.decode()
    assert 'data-testid="home-calendar"' in client.get("/").content.decode()


def test_residents_cannot_manage(client, pilot):
    client.force_login(_u(OWNER))
    for url in ("/community/announcements/", "/community/calendar/new/"):
        assert client.get(url).status_code == 403


def test_isolation(pilot):
    _ann("all", "Ours")
    _event("all", "Our event")
    other = Association.objects.create(name="Other", short_name="O")
    with association_context(other):
        _ann("all", "Theirs")
        assert list(Announcement.objects.values_list("title", flat=True)) == ["Theirs"]
        assert not CalendarEvent.objects.exists()
    assert {a.title for a in services.visible_announcements(pilot, {"all"})} == {"Ours"}
    assert [o.title for o in services.upcoming(other, {"all"})] == []
