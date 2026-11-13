"""Settings > Go live: what the loaded records cover, what the board handles by hand for
the days after them, the readiness checks, and the System admin's Go live button."""

import datetime as dt
from decimal import Decimal as D

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import services
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, BillingRun
from homeoa.billing.services import default_schedule_kwargs
from homeoa.core import status
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.imports.models import ImportRun, Source
from homeoa.jobs.scheduler import PeriodicJob, runs_on
from homeoa.ledger.chart import load_chart_of_accounts

User = get_user_model()

TODAY = dt.date(2026, 10, 15)
URL = "/settings/roles/go-live/"


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        assert not status.is_live(a)
        yield a


@pytest.fixture
def admin(client, assoc):
    usr = User.objects.create_user("admin@example.org")
    grant(usr, "sys_admin", association=assoc)
    client.force_login(usr)
    return usr


def _fresh(assoc):
    return Association.objects.get(pk=assoc.pk)


def _import(assoc, as_of):
    return ImportRun.objects.create(
        association=assoc,
        source=Source.APPFOLIO,
        mode=ImportRun.Mode.COMMIT,
        status=ImportRun.Status.COMMITTED,
        as_of=as_of,
    )


def _schedule(assoc, due_day=10):
    load_chart_of_accounts(assoc)
    return AssessmentSchedule.objects.create(
        association=assoc,
        name="Dues",
        annual_amount=D("12000.00"),
        effective_start=dt.date(2026, 1, 1),
        **{**default_schedule_kwargs(assoc), "due_day": due_day},
    )


def test_a_board_member_without_settings_access_is_refused(client, assoc):
    usr = User.objects.create_user("board@example.org")
    grant(usr, "board", association=assoc)
    client.force_login(usr)
    assert client.get(URL).status_code == 403


def test_a_new_association_without_loaded_records_shows_only_the_people_check(client, assoc, admin):
    page = client.get(URL).content.decode()
    assert "Setting up" in page
    assert 'data-testid="go-live-records"' not in page
    assert 'data-testid="check-people"' in page
    assert 'data-testid="check-import"' not in page
    assert 'data-testid="go-live-form"' in page


def test_dues_that_fell_due_after_the_records_end_are_listed_to_post_by_hand(client, assoc, admin):
    _import(assoc, TODAY - dt.timedelta(days=10))
    _schedule(assoc, due_day=10)
    _schedule(assoc, due_day=15)
    page = client.get(URL).content.decode()
    assert "The loaded records cover through Oct 5, 2026." in page
    assert 'data-testid="gap-warning"' in page
    assert "October 2026 dues (due Oct 10, 2026)" in page
    assert "due Oct 15, 2026" not in page
    assert "between Oct 6, 2026 and Oct 14, 2026" in page
    assert 'data-testid="check-import"' in page


def test_a_month_already_posted_is_not_listed_in_the_gap(client, assoc, admin):
    _import(assoc, TODAY - dt.timedelta(days=10))
    schedule = _schedule(assoc, due_day=10)
    BillingRun.objects.create(
        association=assoc,
        schedule=schedule,
        period=dt.date(2026, 10, 1),
        charge_date=dt.date(2026, 10, 10),
    )
    page = client.get(URL).content.decode()
    assert 'data-testid="gap-warning"' in page
    assert "October 2026 dues" not in page


def test_no_gap_warning_when_the_records_end_yesterday(client, assoc, admin):
    _import(assoc, TODAY - dt.timedelta(days=1))
    page = client.get(URL).content.decode()
    assert "The loaded records cover through Oct 14, 2026." in page
    assert 'data-testid="gap-warning"' not in page


def test_pressing_without_the_confirm_box_changes_nothing(client, assoc, admin):
    assert client.post(URL, {}).status_code == 200
    assert status.go_live_date(_fresh(assoc)) is None


def test_pressing_go_live_makes_the_association_live_today(client, assoc, admin):
    resp = client.post(URL, {"confirm": "on"})
    assert resp.status_code == 302 and resp.url == URL
    assoc = _fresh(assoc)
    assert status.go_live_date(assoc) == TODAY
    assert services.residents_enabled(assoc)
    job = PeriodicJob("t.dues", "0 6 * * *", lambda **k: None, waits_for_go_live=True)
    assert runs_on(job, assoc, TODAY)
    event = AuditEvent.objects.filter(action="association.go_live_date").latest("pk")
    assert event.changes["go_live_date"] == [None, TODAY.isoformat()]
    assert event.actor == admin


def test_pressing_when_already_live_is_refused_and_changes_nothing(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    resp = client.post(URL, {"confirm": "on"}, follow=True)
    assert "already live" in resp.content.decode()
    assert status.go_live_date(_fresh(assoc)) == dt.date(2026, 8, 1)


def test_once_live_the_page_shows_live_since_and_no_button(client, assoc, admin):
    status.set_go_live_date(assoc, dt.date(2026, 8, 1), reason="test")
    page = client.get(URL).content.decode()
    assert "Live since Aug 1, 2026" in page
    assert 'data-testid="go-live-form"' not in page
