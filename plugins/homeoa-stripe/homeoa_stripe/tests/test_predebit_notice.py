"""The pre-debit notice for variable autopay and mandate retention."""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import Charge
from homeoa.billing.services import adjustments as adj
from homeoa.notify import types as ntypes
from homeoa.notify.models import Notification
from homeoa.payments import autopay
from homeoa.payments.forms import AutopaySettingsForm
from homeoa.payments.models import AutopayCharge, AutopayRule, AutopayStatus, NoticeSource
from homeoa_stripe import mandates
from homeoa_stripe.models import MandateRetained, StripeMandate

from .test_pay_and_bank_link import NOV1, NOV10, OCT26, enroll_c, login, mandate_c, post_nov
from .test_stripe import DUES_C, pay

OCT25 = dt.date(2026, 10, 25)


@pytest.fixture
def residents(pilot):
    open_residents(pilot)
    return pilot


def nov_dues(unit_c):
    from homeoa.billing.models import AssessmentSchedule
    from homeoa.billing.services import unit_dues

    return unit_dues(AssessmentSchedule.objects.get(), [unit_c])[unit_c.pk]


def upcoming():
    return Notification.objects.filter(type_key=autopay.UPCOMING_TYPE)


def test_upcoming_type_is_mandatory():
    assert ntypes.get(autopay.UPCOMING_TYPE).mandatory


def test_default_notice_on_25th_debit_on_10th(residents, unit_c):
    """Default: debit on the dues due day (10th), notice 15 days ahead (Oct 26 for Nov 10)."""
    enroll_c(unit_c)
    assert autopay.cycle_dates(residents, NOV1) == (OCT26, NOV10)
    autopay.run(residents, OCT25)
    assert not AutopayCharge.objects.exists()
    autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get(unit=unit_c, period=NOV1)
    assert charge.status == AutopayStatus.SCHEDULED
    assert charge.debit_on == NOV10 and charge.noticed_on == OCT26
    # October is unpaid; November's dues are not posted yet but are projected.
    assert charge.noticed_amount == DUES_C + nov_dues(unit_c)
    assert charge.notice_source == NoticeSource.NOTICE
    note = upcoming().get()
    assert "November 10, 2026" in note.context["body"]
    # Idempotent per enrollment and month.
    autopay.run(residents, dt.date(2026, 10, 27))
    assert AutopayCharge.objects.count() == 1 and upcoming().count() == 1


def test_notice_at_least_10_days_before(residents, unit_c):
    autopay.set_cycle_settings(
        residents, debit_day=10, notice_lead_days=10, as_of=dt.date(2026, 10, 7)
    )
    enroll_c(unit_c)
    autopay.run(residents, dt.date(2026, 10, 30))
    assert not AutopayCharge.objects.exists()
    autopay.run(residents, dt.date(2026, 10, 31))
    charge = AutopayCharge.objects.get()
    assert (charge.debit_on - charge.noticed_on).days == 10


def test_too_late_for_notice_means_no_debit(residents, unit_c):
    enroll_c(unit_c)
    autopay.run(residents, dt.date(2026, 11, 2))  # 8 days before Nov 10: too late
    post_nov(residents)
    assert autopay.run(residents, NOV10) is None
    assert not AutopayCharge.objects.filter(period=NOV1).exists()
    assert not upcoming().exists()


def test_debit_capped_at_noticed_amount(residents, unit_c):
    enroll_c(unit_c)
    autopay.run(residents, OCT26)
    noticed = AutopayCharge.objects.get().noticed_amount
    post_nov(residents)
    adj.manual_charge(unit_c, Decimal("80.00"), "test", "Key fob", date=dt.date(2026, 11, 3))
    autopay.run(residents, NOV10)
    charge = AutopayCharge.objects.get()
    assert charge.status == AutopayStatus.PENDING
    assert charge.amount == noticed  # the $80 waits for the next cycle's notice


def test_debit_takes_less_when_balance_fell(residents, unit_c):
    enroll_c(unit_c)
    autopay.run(residents, OCT26)
    pay(unit_c, DUES_C)  # October paid by hand, still processing
    post_nov(residents)
    autopay.run(residents, NOV10)
    charge = AutopayCharge.objects.get()
    assert charge.amount == Charge.objects.get(unit=unit_c, period=NOV1).amount


def test_fixed_below_balance_needs_no_notice(residents, unit_c):
    enroll_c(unit_c, AutopayRule.FIXED, Decimal("100.00"))
    autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get()
    assert charge.notice_source == NoticeSource.NONE
    assert charge.noticed_amount == Decimal("100.00")
    assert not upcoming().exists()
    post_nov(residents)
    autopay.run(residents, NOV10)
    charge.refresh_from_db()
    assert charge.amount == Decimal("100.00") and charge.status == AutopayStatus.PENDING


def test_fixed_above_balance_needs_no_notice_and_debits_owed(residents, unit_c):
    """Range election: a fixed debit capped at the balance still needs no notice."""
    enroll_c(unit_c, AutopayRule.FIXED, Decimal("5000.00"))
    autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get()
    assert charge.notice_source == NoticeSource.NONE
    assert charge.noticed_amount == Decimal("5000.00")
    assert not upcoming().exists()
    post_nov(residents)
    autopay.run(residents, NOV10)
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.PENDING
    assert charge.amount == DUES_C + nov_dues(unit_c)


def _sent_statement(assoc, unit_c, sent_on, generated_on=dt.date(2026, 10, 1)):
    from homeoa.statements import owner_statements as svc
    from homeoa.statements.statement_models import Statement

    svc.generate(assoc, dt.date(2026, 9, 1), as_of=generated_on)
    st = Statement.objects.get(unit=unit_c)
    sent_at = timezone.make_aware(dt.datetime.combine(sent_on, dt.time(12)))
    Statement.objects.filter(pk=st.pk).update(status=Statement.Status.SENT, sent_at=sent_at)
    st.refresh_from_db()
    return st


def test_statement_serves_as_notice_when_early(residents, unit_c, settings, tmp_path):
    """September's statement prints the Nov 10 debit and serves as its notice."""
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    enroll_c(unit_c)
    st = _sent_statement(residents, unit_c, dt.date(2026, 10, 2))
    assert st.autopay_debit_on == NOV10 and not st.autopay_fixed
    assert autopay.statement_shows_debit_date(st, NOV10)
    autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get()
    assert charge.notice_source == NoticeSource.STATEMENT
    assert charge.notice_statement == st
    assert charge.noticed_amount == st.autopay_amount
    assert not upcoming().exists()


def test_statement_amount_is_projected_not_closing(residents, unit_c, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    enroll_c(unit_c)
    st = _sent_statement(residents, unit_c, dt.date(2026, 10, 2))
    # The balance today (October posted) plus November's projected dues.
    assert st.autopay_amount == DUES_C + nov_dues(unit_c)
    assert st.autopay_amount != st.closing
    from homeoa.statements import owner_statements as svc

    text = svc.statement_autopay_text(st)
    assert "November 10, 2026" in text and f"{st.autopay_amount:,.2f}" in text


def test_statement_sent_late_does_not_serve(residents, unit_c, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    enroll_c(unit_c)
    _sent_statement(residents, unit_c, dt.date(2026, 11, 3))
    autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get()
    assert charge.notice_source == NoticeSource.NOTICE and upcoming().exists()


def test_statement_with_other_date_does_not_serve(residents, unit_c, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    enroll_c(unit_c)
    st = _sent_statement(residents, unit_c, dt.date(2026, 10, 2))
    assert st.autopay_debit_on == NOV10
    # The board moves November's debit to the 15th after the statement went out.
    month = autopay.set_cycle_settings(
        residents, debit_day=15, notice_lead_days=15, as_of=dt.date(2026, 10, 3)
    )
    assert month == NOV1
    autopay.run(residents, dt.date(2026, 10, 31))
    charge = AutopayCharge.objects.get(period=NOV1)
    assert charge.debit_on == dt.date(2026, 11, 15)
    assert charge.notice_source == NoticeSource.NOTICE and upcoming().exists()


def test_fixed_statement_line_is_informational(residents, unit_c, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    enroll_c(unit_c, AutopayRule.FIXED, Decimal("100.00"))
    st = _sent_statement(residents, unit_c, dt.date(2026, 10, 2))
    assert st.autopay_fixed and st.autopay_amount == Decimal("100.00")
    assert st.autopay_debit_on == NOV10
    assert not autopay.statement_shows_debit_date(st, NOV10)
    autopay.run(residents, OCT26)
    charge = AutopayCharge.objects.get()
    assert charge.notice_source == NoticeSource.NONE and charge.notice_statement is None
    assert not upcoming().exists()


def test_statement_without_debit_date_does_not_serve(residents, unit_c, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    _sent_statement(residents, unit_c, dt.date(2026, 10, 20))
    enroll_c(unit_c)
    autopay.run(residents, OCT26)
    assert AutopayCharge.objects.get().notice_source == NoticeSource.NOTICE
    assert upcoming().exists()


def test_board_sets_day_1_lead_respected_across_month(residents, unit_c):
    start = autopay.set_cycle_settings(
        residents, debit_day=1, notice_lead_days=15, as_of=dt.date(2026, 10, 7)
    )
    assert start == NOV1
    assert autopay.cycle_dates(residents, NOV1) == (dt.date(2026, 10, 17), NOV1)
    assert autopay.cycle_dates(residents, dt.date(2026, 12, 1)) == (
        dt.date(2026, 11, 16),
        dt.date(2026, 12, 1),
    )
    enroll_c(unit_c)
    autopay.run(residents, dt.date(2026, 10, 17))
    assert AutopayCharge.objects.get().debit_on == NOV1
    assert AuditEvent.objects.filter(action="payments.autopay_settings_changed").exists()


def test_lead_below_notice_days_rejected(residents):
    with pytest.raises(autopay.SettingsError):
        autopay.set_cycle_settings(
            residents, debit_day=10, notice_lead_days=9, as_of=dt.date(2026, 10, 7)
        )
    form = AutopaySettingsForm({"debit_day": 10, "notice_lead_days": 9}, least=10)
    assert not form.is_valid() and "notice_lead_days" in form.errors
    assert not AutopaySettingsForm({"debit_day": 29, "notice_lead_days": 15}).is_valid()


def test_day_change_mid_cycle_keeps_noticed_date(residents, unit_c):
    enroll_c(unit_c)
    autopay.run(residents, OCT26)
    start = autopay.set_cycle_settings(
        residents, debit_day=20, notice_lead_days=15, as_of=dt.date(2026, 10, 28)
    )
    assert start == dt.date(2026, 12, 1)  # November was already noticed
    assert autopay.cycle_dates(residents, NOV1)[1] == NOV10
    post_nov(residents)
    autopay.run(residents, NOV10)
    charge = AutopayCharge.objects.get(period=NOV1)
    assert charge.debit_on == NOV10 and charge.status == AutopayStatus.PENDING
    assert autopay.cycle_dates(residents, dt.date(2026, 12, 1)) == (
        dt.date(2026, 12, 5),
        dt.date(2026, 12, 20),
    )


def test_settings_page(client, residents):
    login(client, "treasurer@cedarrow.example")
    resp = client.get("/money/payments/autopay/settings/")
    assert resp.status_code == 200
    assert "Oct 26, 2026" in resp.content.decode()
    resp = client.post(
        "/money/payments/autopay/settings/", {"debit_day": "10", "notice_lead_days": "5"}
    )
    assert resp.status_code == 400
    resp = client.post(
        "/money/payments/autopay/settings/", {"debit_day": "1", "notice_lead_days": "15"}
    )
    assert resp.status_code == 302
    residents.refresh_from_db()
    assert autopay.cycle_settings(residents, NOV1) == (1, 15)


# ------------------------------------------------------------------ mandates (Nacha)


def test_revoked_mandate_kept_and_readable(residents, unit_c):
    m = mandate_c(unit_c)
    mandates.revoke_mandate(m)
    kept = StripeMandate.objects.get(pk=m.pk)
    assert kept.revoked_at is not None
    assert kept.authorization_text and kept.accepted_at and kept.owner_id


def test_mandate_delete_refused(residents, unit_c):
    m = mandate_c(unit_c)
    with pytest.raises(MandateRetained):
        m.delete()
    with pytest.raises(MandateRetained), transaction.atomic():
        StripeMandate.objects.filter(pk=m.pk).delete()
    assert StripeMandate.objects.filter(pk=m.pk).exists()


def test_purge_skips_mandates_revoked_under_2_years(residents, unit_c):
    m = mandate_c(unit_c)
    mandates.revoke_mandate(m)
    assert m.pk not in mandates.purgeable().values_list("pk", flat=True)
    later = timezone.now() + dt.timedelta(days=3 * 365)
    assert m.pk in mandates.purgeable(now=later).values_list("pk", flat=True)
    active = StripeMandate.objects.filter(revoked_at__isnull=True)
    assert not mandates.purgeable(active, now=later).exists()


def test_new_mandate_stores_v2_text_with_range(residents, unit_c):
    m = mandate_c(unit_c)
    assert mandates.CURRENT_VERSION == "2026-10-v2"
    assert m.authorization_version == "2026-10-v2"
    assert "any amount from $0.01 up to that fixed amount" in m.authorization_text
