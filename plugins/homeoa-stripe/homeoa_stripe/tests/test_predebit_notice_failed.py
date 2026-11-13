"""A failed pre-debit notice skips the debit and alerts the board."""

import datetime as dt
import logging

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import Charge, LateFee, LateFeePolicy
from homeoa.billing.services import latefees
from homeoa.notify import services as nsvc
from homeoa.notify.models import Notification
from homeoa.payments import autopay
from homeoa.payments.models import AutopayCharge, AutopayStatus
from homeoa.registry.models import Person
from homeoa_stripe.models import StripeIntent

from .test_pay_and_bank_link import NOV1, NOV10, OCT26, OWNER_C, enroll_c, login, post_nov

User = get_user_model()
TREASURER = "treasurer@cedarrow.example"


@pytest.fixture
def residents(pilot):
    open_residents(pilot)
    # Board members get board notices through their person record (seeded: James Okafor).
    assert Person.objects.filter(user=User.objects.get(email=TREASURER)).exists()
    return pilot


@pytest.fixture
def notice_breaks(monkeypatch):
    """The pre-debit notice raises; every other notification works."""
    real = nsvc.notify

    def flaky(type_key, *args, **kwargs):
        if type_key == autopay.UPCOMING_TYPE:
            raise RuntimeError("mail server down")
        return real(type_key, *args, **kwargs)

    monkeypatch.setattr(nsvc, "notify", flaky)


def _skip_cycle(residents, unit_c, capture):
    enroll_c(unit_c)
    with capture(execute=True):
        autopay.run(residents, OCT26)
    return AutopayCharge.objects.get(unit=unit_c, period=NOV1)


def test_failed_notice_skips_debit_and_alerts_board(
    residents, unit_c, notice_breaks, caplog, django_capture_on_commit_callbacks
):
    with caplog.at_level(logging.ERROR):
        charge = _skip_cycle(residents, unit_c, django_capture_on_commit_callbacks)
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    assert "mail server down" in charge.message
    assert "Pre-debit notice" in caplog.text
    assert AuditEvent.objects.filter(action="payments.autopay_notice_failed").exists()
    alert = Notification.objects.filter(type_key=autopay.NOTICE_FAILED_TYPE).first()
    assert "not at fault" in alert.body
    assert not Notification.objects.filter(type_key=autopay.UPCOMING_TYPE).exists()

    # Never debited, and never re-noticed for that month.
    post_nov(residents)
    intents = StripeIntent.objects.filter(unit=unit_c).count()
    with django_capture_on_commit_callbacks(execute=True):
        assert autopay.run(residents, NOV10) is None
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    assert charge.reference == ""
    assert StripeIntent.objects.filter(unit=unit_c).count() == intents
    assert AutopayCharge.objects.filter(unit=unit_c, period=NOV1).count() == 1

    # Board to-do until the board follows up.
    todo = autopay.todo_items(residents)
    assert [i["id"] for i in todo] == [f"autopay-notice-failed-{charge.pk}"]
    assert "Pre-debit notice failed for unit C" in str(todo[0]["title"])
    autopay.acknowledge(charge)
    assert autopay.todo_items(residents) == []


def test_notice_failing_after_queueing_skips_at_debit(
    residents, unit_c, django_capture_on_commit_callbacks
):
    from homeoa.notify.models import Delivery

    enroll_c(unit_c)
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get(unit=unit_c, period=NOV1)
    assert charge.status == AutopayStatus.SCHEDULED
    # The email job later reports the notice undeliverable.
    Delivery.objects.filter(notification__type_key=autopay.UPCOMING_TYPE).update(
        status=Delivery.Status.FAILED, error="mailbox does not exist"
    )
    post_nov(residents)
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(residents, NOV10)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    assert charge.reference == ""
    assert "mailbox does not exist" in charge.message
    assert Notification.objects.filter(type_key=autopay.NOTICE_FAILED_TYPE).exists()


def test_board_alert_failure_is_logged_and_audited(
    residents, unit_c, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    def boom(*args, **kwargs):
        raise RuntimeError("everything down")

    monkeypatch.setattr(nsvc, "notify", boom)
    with caplog.at_level(logging.ERROR):
        charge = _skip_cycle(residents, unit_c, django_capture_on_commit_callbacks)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    assert AuditEvent.objects.filter(action="payments.autopay_notify_failed").exists()
    assert "Notice-failed alert" in caplog.text


def test_history_shows_skipped_cycle(
    client, residents, unit_c, notice_breaks, django_capture_on_commit_callbacks
):
    charge = _skip_cycle(residents, unit_c, django_capture_on_commit_callbacks)
    login(client, OWNER_C)
    html = client.get("/me/pay/autopay/?unit=C").content.decode()
    assert f'data-testid="autopay-charge-{charge.pk}"' in html
    assert "Not debited: notice failed" in html
    login(client, TREASURER)
    html = client.get("/money/payments/autopay/settings/").content.decode()
    assert f'data-testid="autopay-charge-{charge.pk}"' in html
    assert "Not debited: notice failed" in html


def test_late_fee_skips_unit_with_failed_notice(
    residents, unit_c, notice_breaks, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(latefees, "pre_notice_caps", lambda *a: None)  # plain late fees
    _skip_cycle(residents, unit_c, django_capture_on_commit_callbacks)
    post_nov(residents)
    policy = LateFeePolicy.objects.create(
        association=residents,
        effective_start=dt.date(2026, 1, 1),
        **latefees.default_policy_kwargs(residents),
    )
    nov_c = Charge.objects.get(unit=unit_c, period=NOV1)
    late_day = nov_c.due_date + dt.timedelta(days=policy.grace_days + 1)
    latefees.assess(policy, late_day)
    fees = LateFee.objects.filter(kind="late_fee")
    assert not fees.filter(source_charge=nov_c).exists()
    # Other units' November charges still get their fee.
    assert fees.filter(source_charge__period=NOV1).exclude(source_charge__unit=unit_c).exists()
