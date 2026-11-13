"""Board meeting rules from the WA pack (RCW 64.90.445): owner comments come first on every
board agenda, and a meeting held by video must offer a telephone option and roll-call votes."""

import datetime as dt

import pytest

from homeoa.meetings import services
from homeoa.meetings.models import Meeting
from homeoa.meetings.tests.test_meetings import pilot  # noqa: F401

D = dt.date


def test_owner_comment_period_leads_board_agendas(pilot):  # noqa: F811
    board = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=D(2026, 11, 12))
    services.add_agenda_item(board, title="Approve minutes")
    lines = services.agenda_text(board).splitlines()
    assert lines[0].startswith("1. Owner comments on agenda items (at least 15 minutes")
    assert lines[1] == "2. Approve minutes"
    annual = services.create_meeting(pilot, kind=Meeting.Kind.ANNUAL, date=D(2026, 12, 3))
    assert services.owner_comment_text(annual) == ""


def test_video_meeting_needs_a_phone_option(pilot):  # noqa: F811
    m = services.create_meeting(
        pilot, kind=Meeting.Kind.BOARD, date=D(2026, 11, 12), video_url="https://meet.example/x"
    )
    assert "RCW 64.90.445(3)(d)" in services.remote_problem(m)
    with pytest.raises(services.MeetingError, match="join by phone"):
        services.send_notice(m, services.MODE_PRINT, today=D(2026, 10, 20))
    services.update_meeting(m, dial_in="206-555-0199, code 4411")
    assert services.remote_problem(m) == ""
    _subject, body = services.notice_text(m)
    assert "by phone: 206-555-0199, code 4411" in body
    assert "taken by roll call" in body


def test_in_person_meetings_need_no_phone(pilot):  # noqa: F811
    m = services.create_meeting(pilot, kind=Meeting.Kind.BOARD, date=D(2026, 11, 12))
    assert services.remote_problem(m) == ""
    assert "roll call" not in services.notice_text(m)[1]
