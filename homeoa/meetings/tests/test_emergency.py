"""Emergency board meetings."""

import datetime as dt

import pytest

from homeoa import rules
from homeoa.audit.models import AuditEvent
from homeoa.letters.models import Letter
from homeoa.meetings import services
from homeoa.meetings.models import Meeting, MeetingNotice
from homeoa.meetings.tests.test_meetings import (  # noqa: F401
    TODAY,
    User,
    _consent,
    _go_live,
    _units_with_owners,
    pilot,
)
from homeoa.rules.base import Basis, RulePack, RuleValue
from homeoa.rules.keys import KEYS

D = dt.date
OCT20 = D(2026, 10, 20)
REASON = "Burst pipe flooding units B and C"


def _board(a, date=OCT20, **kw):
    return services.create_meeting(a, kind=Meeting.Kind.BOARD, date=date, **kw)


def _pack(monkeypatch, normal=5, emergency=1, to_known=None):
    values = {k: RuleValue(value=None, basis=Basis.BOARD) for k in KEYS}
    values["notices.board_meeting.emergency_to_known_addresses"] = RuleValue(
        value=to_known, basis=Basis.STATUTE
    )
    values["notices.board_meeting.min_days"] = RuleValue(value=normal, basis=Basis.STATUTE)
    values["notices.board_meeting.emergency_min_days"] = RuleValue(
        value=emergency, basis=Basis.STATUTE, citation="Test 9.9"
    )
    pack = RulePack(id="test", name="Test", jurisdiction="XX", values=values)
    monkeypatch.setattr(rules, "active_pack", lambda association: pack)


def test_generic_pack_has_policy_placeholder():
    from homeoa.rules.packs.generic import pack

    rv = pack.get("notices.board_meeting.emergency_min_days")
    assert rv.value is not None and rv.basis == Basis.POLICY and not rv.verified


def test_window_uses_emergency_key(pilot, monkeypatch):  # noqa: F811
    _pack(monkeypatch)
    m = _board(pilot)
    assert services.notice_window(m, TODAY).latest == D(2026, 10, 15)
    services.set_emergency(m, True, REASON)
    w = services.notice_window(m, TODAY)
    assert w.latest == D(2026, 10, 19)
    assert w.min_rule.key == "notices.board_meeting.emergency_min_days"
    assert w.min_rule.citation == "Test 9.9"
    assert services.window_problem(m, D(2026, 10, 18)) == ""


def test_flag_needs_reason_and_is_audited(pilot):  # noqa: F811
    m = _board(pilot)
    with pytest.raises(services.MeetingError, match="unforeseeable"):
        services.set_emergency(m, True, "  ")
    with pytest.raises(services.MeetingError, match="unforeseeable"):
        _board(pilot, emergency=True)
    services.set_emergency(m, True, REASON)
    m.refresh_from_db()
    assert m.emergency and m.emergency_reason == REASON
    ev = AuditEvent.objects.filter(action="meeting.emergency").latest("pk")
    assert ev.changes["reason"] == REASON
    services.set_emergency(m, False)
    m.refresh_from_db()
    assert not m.emergency and m.emergency_reason == ""
    created = _board(pilot, emergency=True, emergency_reason=REASON)
    assert created.emergency


def test_flag_not_allowed_on_owner_meetings(pilot):  # noqa: F811
    m = services.create_meeting(pilot, kind=Meeting.Kind.ANNUAL, date=D(2026, 11, 20))
    with pytest.raises(services.MeetingError, match="Only a board meeting"):
        services.set_emergency(m, True, REASON)
    with pytest.raises(services.MeetingError, match="Only a board meeting"):
        services.create_meeting(
            pilot,
            kind=Meeting.Kind.SPECIAL,
            date=D(2026, 11, 20),
            emergency=True,
            emergency_reason=REASON,
        )


def test_letters_not_generated(pilot, monkeypatch, django_capture_on_commit_callbacks):  # noqa: F811
    _pack(monkeypatch)
    _go_live(pilot)
    _consent("A")
    m = _board(pilot, date=TODAY + dt.timedelta(days=2))
    services.set_emergency(m, True, REASON)
    with pytest.raises(services.MeetingError, match="email only"):
        services.send_notice(m, services.MODE_PRINT)
    with django_capture_on_commit_callbacks(execute=True):
        result = services.send_notice(m, services.MODE_OWNERS)
    assert result.email >= 1 and result.mail == 0 and result.batch is None
    assert result.email + result.no_consent + result.skipped >= len(_units_with_owners(pilot))
    assert result.no_consent >= 1  # Counted apart from "already noticed"
    assert not Letter.objects.exists()
    assert set(MeetingNotice.objects.values_list("channel", flat=True)) == {"email"}
    assert REASON in MeetingNotice.objects.first().body
    m.refresh_from_db()
    assert m.status == Meeting.Status.NOTICED
    with pytest.raises(services.MeetingError):
        services.set_emergency(m, False)


def test_views(client, pilot):  # noqa: F811
    client.force_login(User.objects.get(email="secretary@cedarrow.example"))
    m = _board(pilot)
    url = f"/governance/meetings/{m.pk}/emergency/"
    client.post(url, {"on": "1", "reason": ""})
    m.refresh_from_db()
    assert not m.emergency
    client.post(url, {"on": "1", "reason": REASON})
    m.refresh_from_db()
    assert m.emergency
    page = client.get(f"/governance/meetings/{m.pk}/").content.decode()
    assert 'data-testid="meeting-emergency"' in page and REASON in page
    assert 'data-testid="emergency-note"' in page


def test_known_addresses_get_the_notice_without_consent(
    pilot,  # noqa: F811
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    """WA (RCW 64.90.445(2)(f)): emergency notice goes to every owner with a known email."""
    _pack(monkeypatch, to_known=True)
    _go_live(pilot)
    m = _board(pilot, date=TODAY + dt.timedelta(days=2))
    services.set_emergency(m, True, REASON)
    with django_capture_on_commit_callbacks(execute=True):
        result = services.send_notice(m, services.MODE_OWNERS)
    from homeoa.registry.services import current_owners

    units = _units_with_owners(pilot)
    with_email = [u for u in units if any(p.email for p in current_owners(u, TODAY))]
    assert result.no_consent == 0 and result.mail == 0
    assert (result.email, result.no_email) == (len(with_email), len(units) - len(with_email))
    assert not Letter.objects.exists()
