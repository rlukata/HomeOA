"""Association status: Setting up until the go-live date, then Live; resident access opens
at go-live unless the System admin has paused it."""

import datetime as dt
import importlib

import pytest
from django.apps import apps as django_apps

from homeoa.audit.models import AuditEvent
from homeoa.core import residents_enabled, status
from homeoa.core.context import association_context
from homeoa.core.models import Association, AssociationStatus

TODAY = dt.date(2026, 10, 15)


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def _fresh(assoc):
    return Association.objects.get(pk=assoc.pk)


def test_without_a_status_row_the_association_is_setting_up_with_residents_off(assoc):
    assert not AssociationStatus.all_associations.exists()
    assert not status.is_live(assoc)
    assert str(status.label(assoc)) == "Setting up"
    assert status.go_live_date(assoc) is None
    assert not residents_enabled(assoc)


def test_no_association_means_residents_off():
    assert residents_enabled(None) is False


def test_go_live_in_the_future_is_still_setting_up(assoc):
    status.set_go_live_date(assoc, TODAY + dt.timedelta(days=1), reason="Planned cutover")
    assoc = _fresh(assoc)
    assert not status.is_live(assoc)
    assert not residents_enabled(assoc)
    assert status.is_live(assoc, on=TODAY + dt.timedelta(days=1))


@pytest.mark.parametrize("go_live", [TODAY, TODAY - dt.timedelta(days=30)])
def test_go_live_today_or_earlier_is_live_with_residents_on(assoc, go_live):
    status.set_go_live_date(assoc, go_live, reason="Cutover")
    assoc = _fresh(assoc)
    assert status.is_live(assoc)
    assert str(status.label(assoc)) == "Live"
    assert residents_enabled(assoc)


def test_pausing_closes_resident_access_while_live(assoc, django_user_model):
    admin = django_user_model.objects.create_user("admin@example.com")
    status.set_go_live_date(assoc, TODAY, reason="Cutover")
    assert status.pause_residents(assoc, reason="Wrong balances shown", by=admin)
    assoc = _fresh(assoc)
    assert status.is_live(assoc)
    assert not residents_enabled(assoc)
    row = status.status_for(assoc)
    assert row.pause_reason == "Wrong balances shown"
    assert row.paused_by == admin and row.paused_at is not None
    assert status.resume_residents(assoc, reason="Fixed")
    assert residents_enabled(_fresh(assoc))


def test_pause_and_resume_are_audited_and_report_no_change(assoc):
    status.set_go_live_date(assoc, TODAY, reason="Cutover")
    assert status.pause_residents(assoc, reason="Emergency")
    assert not status.pause_residents(assoc, reason="Again")
    assert status.resume_residents(assoc, reason="All clear")
    assert not status.resume_residents(assoc, reason="Again")
    events = list(AuditEvent.objects.filter(action="association.residents_enabled").order_by("pk"))
    assert [e.changes["reason"] for e in events] == ["Emergency", "All clear"]
    assert events[0].changes["residents_enabled"] == [True, False]
    assert events[1].changes["residents_enabled"] == [False, True]


@pytest.mark.parametrize("reason", ["", "   "])
def test_pause_and_resume_need_a_reason(assoc, reason):
    with pytest.raises(ValueError):
        status.pause_residents(assoc, reason=reason)
    with pytest.raises(ValueError):
        status.resume_residents(assoc, reason=reason)
    with pytest.raises(ValueError):
        status.set_go_live_date(assoc, TODAY, reason=reason)


def test_setting_the_go_live_date_is_audited(assoc):
    assert status.set_go_live_date(assoc, TODAY, reason="Board adopted cutover")
    assert not status.set_go_live_date(assoc, TODAY, reason="Same date")
    event = AuditEvent.objects.get(action="association.go_live_date")
    assert event.changes["go_live_date"] == [None, TODAY.isoformat()]
    assert event.changes["reason"] == "Board adopted cutover"


def _migration():
    return importlib.import_module("homeoa.core.migrations.0002_association_status")


@pytest.mark.parametrize("was_on", [True, False])
def test_migration_keeps_resident_access_as_the_old_switch_had_it(db, settings, was_on):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(
        name="Old HOA",
        short_name="OH",
        timezone="America/Los_Angeles",
        settings={"residents_enabled": was_on},
    )
    # Created late on Jan 1 Pacific time, which is already Jan 2 in UTC.
    created = dt.datetime(2026, 1, 2, 6, 0, tzinfo=dt.UTC)
    Association.objects.filter(pk=a.pk).update(created_at=created)
    _migration().create_statuses(django_apps, None)
    a = _fresh(a)
    row = AssociationStatus.all_associations.get(association=a)
    assert row.go_live_date == dt.date(2026, 1, 1)
    assert status.is_live(a)
    assert row.residents_paused is not was_on
    assert residents_enabled(a) is was_on
    assert a.settings == {"residents_enabled": was_on}


def test_going_live_today_sets_todays_date_and_is_audited_under_the_user(assoc):
    from django.contrib.auth import get_user_model

    usr = get_user_model().objects.create_user("admin@example.org")
    assert status.go_live_today(assoc, by=usr) == TODAY
    assoc = _fresh(assoc)
    assert status.is_live(assoc) and residents_enabled(assoc)
    event = AuditEvent.objects.filter(action="association.go_live_date").latest("pk")
    assert event.actor == usr
    assert event.changes["reason"] == status.GO_LIVE_REASON


def test_going_live_today_replaces_a_future_go_live_date(assoc):
    status.set_go_live_date(assoc, dt.date(2026, 12, 1), reason="test")
    status.go_live_today(assoc)
    assert status.go_live_date(_fresh(assoc)) == TODAY


def test_going_live_when_already_live_is_refused(assoc):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    with pytest.raises(status.AlreadyLive):
        status.go_live_today(assoc)
    assert status.go_live_date(_fresh(assoc)) == dt.date(2026, 8, 1)
