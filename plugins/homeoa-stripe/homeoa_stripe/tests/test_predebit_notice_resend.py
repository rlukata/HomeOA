"""Resend a failed pre-debit notice and reschedule the debit."""

import datetime as dt

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
NOV5 = dt.date(2026, 11, 5)
NOV15 = dt.date(2026, 11, 15)
NOV25 = dt.date(2026, 11, 25)
DEC5 = dt.date(2026, 12, 5)


@pytest.fixture
def residents(pilot):
    open_residents(pilot)
    # The demo seed links the treasurer login to an owner Person.
    assert Person.objects.filter(user=User.objects.get(email=TREASURER)).exists()
    return pilot


@pytest.fixture
def mail(monkeypatch):
    """Toggle: while ``mail["down"]`` the pre-debit notice raises."""
    state = {"down": True}
    real = nsvc.notify

    def flaky(type_key, *args, **kwargs):
        if type_key == autopay.UPCOMING_TYPE and state["down"]:
            raise RuntimeError("mail server down")
        return real(type_key, *args, **kwargs)

    monkeypatch.setattr(nsvc, "notify", flaky)
    return state


@pytest.fixture
def skipped(residents, unit_c, mail, django_capture_on_commit_callbacks):
    enroll_c(unit_c)
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get(unit=unit_c, period=NOV1)
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    return charge


def _resend(charge, as_of, capture, user=None):
    with capture(execute=True):
        return autopay.resend_notice(charge, as_of=as_of, user=user)


def test_resend_reschedules_and_debits_after_notice_days(
    residents, unit_c, skipped, mail, django_capture_on_commit_callbacks
):
    mail["down"] = False
    post_nov(residents)
    charge = _resend(skipped, NOV5, django_capture_on_commit_callbacks)
    assert charge.status == AutopayStatus.SCHEDULED
    assert charge.debit_on == NOV15  # 10 days after the resend, later than Nov 10
    assert charge.noticed_on == NOV5
    assert charge.noticed_amount > 0
    assert charge.notice_resends == 1
    assert charge.message == ""
    notice = Notification.objects.get(type_key=autopay.UPCOMING_TYPE)
    assert "November 15, 2026" in notice.body
    assert AuditEvent.objects.filter(action="payments.autopay_notice_resent").exists()
    assert autopay.todo_items(residents) == []
    # Still excused from late fees: the owner was not at fault.
    assert (unit_c.pk, NOV1) in latefees.notice_failed_months(residents)

    with django_capture_on_commit_callbacks(execute=True):
        assert autopay.run(residents, NOV10) is None
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(residents, NOV15)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.PENDING
    assert charge.amount <= charge.noticed_amount
    assert StripeIntent.objects.filter(unit=unit_c).count() == 1


def test_debit_moved_into_next_month_is_still_collected(
    residents, unit_c, skipped, mail, django_capture_on_commit_callbacks
):
    mail["down"] = False
    post_nov(residents)
    charge = _resend(skipped, NOV25, django_capture_on_commit_callbacks)
    assert charge.debit_on == DEC5
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(residents, DEC5)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.PENDING


def test_resend_failing_again_stays_skipped_with_new_alert(
    residents, unit_c, skipped, django_capture_on_commit_callbacks
):
    alerts = Notification.objects.filter(type_key=autopay.NOTICE_FAILED_TYPE)
    first = alerts.count()
    assert first >= 1
    charge = _resend(skipped, NOV5, django_capture_on_commit_callbacks)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    assert charge.notice_resends == 1
    assert "mail server down" in charge.message
    assert alerts.count() == 2 * first
    assert [i["id"] for i in autopay.todo_items(residents)] == [
        f"autopay-notice-failed-{charge.pk}"
    ]
    with django_capture_on_commit_callbacks(execute=True):
        autopay.run(residents, NOV15)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.SKIPPED_NOTICE_FAILED
    assert not StripeIntent.objects.filter(unit=unit_c).exists()


def test_resend_refuses_other_states(residents, unit_c, skipped, mail):
    mail["down"] = False
    autopay.resend_notice(skipped, as_of=NOV5)
    with pytest.raises(autopay.ResendError):
        autopay.resend_notice(skipped, as_of=NOV5)


def test_resend_refuses_cancelled_enrollment(residents, skipped):
    autopay.cancel(skipped.enrollment)
    with pytest.raises(autopay.ResendError):
        autopay.resend_notice(skipped, as_of=NOV5)


def test_resend_view_needs_finance_manage(client, residents, skipped):
    login(client, OWNER_C)
    assert client.post(f"/money/payments/autopay/{skipped.pk}/resend/").status_code == 403
    skipped.refresh_from_db()
    assert skipped.status == AutopayStatus.SKIPPED_NOTICE_FAILED


def test_todo_and_history_resend(client, residents, unit_c, skipped, mail, monkeypatch):
    from homeoa.payments import autopay_views as views

    mail["down"] = False
    monkeypatch.setattr(views, "association_today", lambda a: NOV5)
    todo = autopay.todo_items(residents)[0]
    assert todo["post_url"] == f"/money/payments/autopay/{skipped.pk}/resend/"
    login(client, TREASURER)
    html = client.get("/money/payments/autopay/settings/").content.decode()
    assert f'data-testid="autopay-resend-{skipped.pk}"' in html
    resp = client.post(todo["post_url"], {"next": "settings"})
    assert resp.status_code == 302
    assert resp.url.endswith("/money/payments/autopay/settings/")
    skipped.refresh_from_db()
    assert skipped.status == AutopayStatus.SCHEDULED
    assert skipped.debit_on == NOV15
    event = AuditEvent.objects.get(action="payments.autopay_notice_resent")
    assert event.actor.email == TREASURER


def _late_fee_for_c(residents, unit_c, monkeypatch):
    monkeypatch.setattr(latefees, "pre_notice_caps", lambda *a: None)  # plain late fees
    policy = LateFeePolicy.objects.create(
        association=residents,
        effective_start=dt.date(2026, 1, 1),
        **latefees.default_policy_kwargs(residents),
    )
    nov_c = Charge.objects.get(unit=unit_c, period=NOV1)
    latefees.assess(policy, nov_c.due_date + dt.timedelta(days=policy.grace_days + 1))
    return LateFee.objects.filter(kind="late_fee", source_charge=nov_c).exists()


def _resend_and_debit(residents, skipped, mail, capture):
    mail["down"] = False
    post_nov(residents)
    _resend(skipped, NOV5, capture)
    with capture(execute=True):
        autopay.run(residents, NOV15)
    skipped.refresh_from_db()
    assert skipped.status == AutopayStatus.PENDING
    return skipped


def test_resent_and_collected_cycle_gets_no_late_fee(
    residents, unit_c, skipped, mail, monkeypatch, django_capture_on_commit_callbacks
):
    _resend_and_debit(residents, skipped, mail, django_capture_on_commit_callbacks)
    assert not _late_fee_for_c(residents, unit_c, monkeypatch)


def test_resent_debit_declined_gets_normal_late_fee(
    residents, unit_c, skipped, mail, monkeypatch, django_capture_on_commit_callbacks
):
    charge = _resend_and_debit(residents, skipped, mail, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        autopay.reference_failed(residents, charge.reference, "insufficient_funds")
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.FAILED
    assert (unit_c.pk, NOV1) not in latefees.notice_failed_months(residents)
    assert _late_fee_for_c(residents, unit_c, monkeypatch)
