"""Pay now, bank linking and autopay against the fake Stripe client."""

import datetime as dt
import re
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import open_residents
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.jobs.scheduler import periodic_jobs, run_due
from homeoa.notify.models import Notification
from homeoa.payments import autopay
from homeoa.payments.models import AutopayCharge, AutopayEnrollment, AutopayRule, AutopayStatus
from homeoa.payments.services import PaymentError
from homeoa.registry.models import Person, Unit
from homeoa_stripe import fake
from homeoa_stripe.models import IntentStatus, StripeIntent, StripeMandate

from .test_stripe import DUES_C, SECRET, pay, post

User = get_user_model()
OWNER_C = "ownerc@cedarrow.example"
OWNER_G = "owner7@cedarrow.example"
OCT7 = dt.date(2026, 10, 7)
NOV1 = dt.date(2026, 11, 1)
OCT26 = dt.date(2026, 10, 26)  # Notice 15 days before
NOV10 = dt.date(2026, 11, 10)  # Debit on the dues due day


@pytest.fixture
def residents(pilot):
    open_residents(pilot)
    return pilot


def login(client, email):
    client.force_login(User.objects.get(email=email))


def person(email):
    return Person.objects.get(user__email=email)


def mandate_c(unit_c):
    return StripeMandate.objects.filter(unit=unit_c, verified_at__isnull=False).first()


def enroll_c(unit_c, rule=AutopayRule.BALANCE, fixed=None):
    return autopay.enroll(
        unit_c,
        person(OWNER_C),
        rule=rule,
        fixed_amount=fixed,
        mandate_ref=str(mandate_c(unit_c).pk),
        as_of=OCT7,
    )


def post_nov(pilot):
    generate_assessments(AssessmentSchedule.objects.get(), NOV1, source="test")


def notice_then_debit(assoc, before_debit=None):
    """Notice on Oct 26, post November, then debit on Nov 10."""
    autopay.run(assoc, OCT26)
    post_nov(assoc)
    if before_debit:
        before_debit()
    return autopay.run(assoc, NOV10)


def confirm_key(client, unit="C", amount=None):
    url = f"/me/pay/confirm/?unit={unit}" + (f"&amount={amount}" if amount else "")
    resp = client.get(url)
    assert resp.status_code == 200, resp
    return re.search(r'name="key" value="([^"]+)"', resp.content.decode()).group(1), resp


# ------------------------------------------------------------------ pay now


def test_pay_page_shows_due_and_bank(client, residents, unit_c):
    login(client, OWNER_C)
    html = client.get("/me/pay/").content.decode()
    assert f"Pay ${DUES_C}" in html
    assert 'data-testid="bank-accounts"' in html
    assert "no fee" in html
    assert 'data-testid="free-alternative"' not in html


def test_pay_now_success_and_double_submit(client, residents, unit_c):
    login(client, OWNER_C)
    key, resp = confirm_key(client)
    assert f"${DUES_C}" in resp.content.decode()
    ref = str(mandate_c(unit_c).pk)
    data = {"unit": "C", "amount": str(DUES_C), "key": key, "method": ref}
    assert client.post("/me/pay/confirm/", data).status_code == 302
    assert client.post("/me/pay/confirm/", data).status_code == 302
    intents = StripeIntent.objects.filter(unit=unit_c)
    assert intents.count() == 1 and intents.get().status == IntentStatus.PROCESSING
    html = client.get("/me/pay/").content.decode()
    assert 'data-testid="pending-payments"' in html
    assert 'data-testid="amount-due">$0.00' in html


def test_pay_now_failure_shows_error(client, residents, unit_c):
    login(client, OWNER_C)
    key, _ = confirm_key(client)
    fake.fail_next()
    data = {"unit": "C", "amount": str(DUES_C), "key": key, "method": str(mandate_c(unit_c).pk)}
    resp = client.post("/me/pay/confirm/", data, follow=True)
    assert "did not go through" in resp.content.decode()
    assert not StripeIntent.objects.filter(unit=unit_c, status=IntentStatus.PROCESSING).exists()


def test_pay_another_amount_at_balance_ok(client, residents, unit_c):
    login(client, OWNER_C)
    _, resp = confirm_key(client, amount=str(DUES_C))
    assert f"${DUES_C}" in resp.content.decode()


@pytest.mark.parametrize("extra", ["0.01", "1.00", "5000.00"])
def test_pay_another_amount_above_balance_refused(client, residents, unit_c, extra):
    """No prepayment; the error shows the maximum and nothing reaches Stripe."""
    login(client, OWNER_C)
    amount = DUES_C + Decimal(extra)
    resp = client.get(f"/me/pay/confirm/?unit=C&amount={amount}", follow=True)
    assert f"You can pay at most your balance of ${DUES_C}" in resp.content.decode()
    key = "k" * 16
    data = {"unit": "C", "amount": str(amount), "key": key, "method": str(mandate_c(unit_c).pk)}
    client.post("/me/pay/confirm/", data)
    assert not StripeIntent.objects.filter(unit=unit_c).exists()


@pytest.mark.parametrize("amount", ["0", "-5.00"])
def test_pay_another_amount_zero_or_negative_refused(client, residents, unit_c, amount):
    login(client, OWNER_C)
    resp = client.get(f"/me/pay/confirm/?unit=C&amount={amount}", follow=True)
    assert resp.redirect_chain
    assert 'name="key"' not in resp.content.decode()


def test_credit_balance_has_no_pay_action(client, residents, unit_c):
    _credit_c(client, residents, unit_c)
    login(client, OWNER_C)
    html = client.get("/me/pay/").content.decode()
    assert "You have a credit" in html
    assert 'action="/me/pay/confirm/"' not in html
    resp = client.get("/me/pay/confirm/?unit=C&amount=1.00", follow=True)
    assert "Nothing is due right now" in resp.content.decode()


def test_pass_through_fee_shows_free_alternative(client, residents, unit_c):
    from homeoa.plugins import services as plugins

    plugins.save_installation(
        residents,
        "stripe",
        enabled=True,
        data={"webhook_secret": SECRET, "fee_mode": "pass_through"},
    )
    login(client, OWNER_C)
    html = client.get("/me/pay/").content.decode()
    fee = fake.ach_fee(DUES_C)
    assert f"${fee} fee" in html
    assert 'data-testid="free-alternative"' in html
    assert "pay by check" in html
    _, resp = confirm_key(client)
    assert 'data-testid="confirm-fee"' in resp.content.decode()


def test_preview_has_no_pay_action(client, pilot, unit_c):
    login(client, "treasurer@cedarrow.example")
    html = client.get("/units/C/preview/pay/").content.decode()
    assert "/me/pay/confirm/" not in html
    assert 'name="key"' not in html


# ------------------------------------------------------------------ bank linking


def _link(client, mode):
    assert client.get("/plugins/stripe/bank/?unit=G").status_code == 200
    resp = client.post("/plugins/stripe/bank/collect/", {"unit": "G", "mode": mode}, follow=True)
    assert "could not be linked" not in resp.content.decode()
    resp = client.post(
        "/plugins/stripe/bank/confirm/",
        {"unit": "G", "accept": "yes"},
        follow=True,
        HTTP_USER_AGENT="pytest",
    )
    assert StripeMandate.objects.filter(unit__number="G").exists(), [
        str(m) for m in resp.context["messages"]
    ]
    return resp


def test_bank_link_instant(client, residents):
    login(client, OWNER_G)
    _link(client, "instant")
    m = StripeMandate.objects.get(unit__number="G")
    assert m.chargeable


def test_bank_link_microdeposits(client, residents):
    login(client, OWNER_G)
    _link(client, "manual")
    m = StripeMandate.objects.get(unit__number="G")
    assert not m.chargeable
    url = f"/plugins/stripe/bank/{m.pk}/verify/"
    client.post(url, {"amount1": "1", "amount2": "2"})
    m.refresh_from_db()
    assert not m.chargeable
    client.post(url, {"amount1": "32", "amount2": "45"})
    m.refresh_from_db()
    assert m.chargeable


def test_revoke_cancels_autopay(client, residents, unit_c):
    enrollment = enroll_c(unit_c)
    login(client, OWNER_C)
    client.post(f"/plugins/stripe/bank/{mandate_c(unit_c).pk}/remove/")
    enrollment.refresh_from_db()
    assert enrollment.cancelled_at is not None


# ------------------------------------------------------------------ autopay


def test_autopay_enroll_view_starts_next_month(client, residents, unit_c):
    login(client, OWNER_C)
    resp = client.post(
        "/me/pay/autopay/",
        {"unit": "C", "action": "on", "rule": "balance", "method": str(mandate_c(unit_c).pk)},
    )
    assert resp.status_code == 302
    e = AutopayEnrollment.objects.get(unit=unit_c, cancelled_at__isnull=True)
    assert e.starts_on == NOV1
    assert autopay.run(residents, OCT7) is None  # October is paid by hand


def test_job_ordering_and_idempotent(residents, unit_c):
    names = list(periodic_jobs())
    assert names.index("billing.post_assessments") < names.index("payments.autopay")
    enroll_c(unit_c)
    assert autopay.run(residents, OCT26) is None  # the notice, no debit yet
    assert AutopayCharge.objects.get().status == AutopayStatus.SCHEDULED
    pay(unit_c, DUES_C)  # October settled-in-flight; November is what remains
    run_due(
        as_of=NOV10, association=residents, names=["billing.post_assessments", "payments.autopay"]
    )
    charge = AutopayCharge.objects.get(unit=unit_c, period=NOV1)
    assert charge.status == AutopayStatus.PENDING
    nov = Charge.objects.get(unit=unit_c, period=NOV1)
    assert charge.amount == nov.amount
    autopay.run(residents, dt.date(2026, 11, 11))
    assert AutopayCharge.objects.filter(unit=unit_c).count() == 1


def _credit_c(client, residents, unit_c):
    intent = pay(unit_c, Decimal("3000.00"))
    post(client, residents, intent)


@pytest.mark.parametrize("rule", [AutopayRule.BALANCE, AutopayRule.FIXED])
def test_nothing_owed_is_skipped(client, residents, unit_c, rule):
    _credit_c(client, residents, unit_c)
    enroll_c(unit_c, rule, Decimal("100.00") if rule == AutopayRule.FIXED else None)
    assert notice_then_debit(residents) is None
    assert AutopayCharge.objects.get(unit=unit_c).status == AutopayStatus.SKIPPED


def _settle_october(client, residents, unit_c):
    post(client, residents, pay(unit_c, DUES_C))


def test_fixed_above_balance_debits_balance(client, residents, unit_c):
    """A fixed amount is capped at the balance read at debit time."""
    _settle_october(client, residents, unit_c)
    enroll_c(unit_c, AutopayRule.FIXED, Decimal("5000.00"))
    notice_then_debit(residents)
    charge = AutopayCharge.objects.get(unit=unit_c)
    nov = Charge.objects.get(unit=unit_c, period=NOV1)
    assert charge.status == AutopayStatus.PENDING and charge.amount == nov.amount


def test_fixed_below_balance_debits_fixed(client, residents, unit_c):
    _settle_october(client, residents, unit_c)
    enroll_c(unit_c, AutopayRule.FIXED, Decimal("100.00"))
    notice_then_debit(residents)
    charge = AutopayCharge.objects.get(unit=unit_c)
    assert charge.status == AutopayStatus.PENDING and charge.amount == Decimal("100.00")


def test_fixed_zero_balance_skips(client, residents, unit_c):
    _settle_october(client, residents, unit_c)
    enroll_c(unit_c, AutopayRule.FIXED, Decimal("100.00"))

    def pay_nov():
        nov = Charge.objects.get(unit=unit_c, period=NOV1)
        post(client, residents, pay(unit_c, nov.amount))

    assert notice_then_debit(residents, pay_nov) is None
    assert AutopayCharge.objects.get(unit=unit_c).status == AutopayStatus.SKIPPED


def test_autopay_form_says_fixed_is_capped(client, residents, unit_c):
    login(client, OWNER_C)
    html = client.get("/me/pay/autopay/?unit=C").content.decode()
    assert "never more than your balance" in html


def test_failure_notifies_and_todos(residents, unit_c, django_capture_on_commit_callbacks):
    # Board members get the board notice through their person record (as owners do);
    # the demo seed links the treasurer login to James Okafor (unit B).
    assert Person.objects.filter(user__email="treasurer@cedarrow.example").exists()
    enroll_c(unit_c)
    with django_capture_on_commit_callbacks(execute=True):
        notice_then_debit(residents, fake.fail_next)
    charge = AutopayCharge.objects.get(unit=unit_c)
    assert charge.status == AutopayStatus.FAILED
    assert Notification.objects.filter(type_key=autopay.OWNER_TYPE).exists()
    assert Notification.objects.filter(type_key=autopay.BOARD_TYPE).exists()
    assert [i["id"] for i in autopay.todo_items(residents)] == [f"autopay-failed-{charge.pk}"]
    autopay.acknowledge(charge)
    assert autopay.todo_items(residents) == []
    # Not retried automatically.
    autopay.run(residents, dt.date(2026, 11, 11))
    assert AutopayCharge.objects.filter(unit=unit_c).count() == 1


def test_webhook_failure_marks_charge_failed(client, residents, unit_c):
    enroll_c(unit_c)
    notice_then_debit(residents)
    charge = AutopayCharge.objects.get(unit=unit_c)
    intent = StripeIntent.objects.get(intent_id=charge.reference)
    post(client, residents, intent, "payment_intent.payment_failed")
    charge.refresh_from_db()
    assert charge.status == AutopayStatus.FAILED
    assert autopay.todo_items(residents)


def test_residents_off_blocks_enroll_and_job(pilot, unit_c):
    with pytest.raises(PaymentError):
        enroll_c(unit_c)
    post_nov(pilot)
    assert autopay.run(pilot, NOV1) is None
    assert Unit.objects.filter(number="C").exists()


def test_stripe_js_bank_link_has_visible_error_box(client, residents, monkeypatch, settings):
    """Live Stripe.js path: bank.js shows failures in a hidden alert instead of failing silently."""
    from homeoa_stripe import views

    monkeypatch.setattr(views, "_is_fake", lambda: False)
    settings.HOMEOA_STRIPE_PUBLISHABLE_KEY = "pk_test_123"
    login(client, OWNER_G)
    html = client.get("/plugins/stripe/bank/?unit=G").content.decode()
    assert 'id="collect-bank"' in html and 'data-publishable-key="pk_test_123"' in html
    assert re.search(r'data-error-text="Bank linking didn(&#x27;|\')t work', html)
    box = r'<p class="callout danger" id="collect-bank-error" role="alert"[^>]* hidden>'
    assert re.search(box, html)


@pytest.mark.parametrize("name", ["pay", "payments", "statements"])
def test_money_pages_use_tabs_with_current_page(client, residents, name):
    from django.urls import reverse

    login(client, OWNER_C)
    html = client.get(reverse(f"portal:{name}")).content.decode()
    nav = re.search(r'<nav class="tabs" aria-label="Money pages">(.*?)</nav>', html, re.S).group(1)
    current = re.findall(r'<a href="([^"]+)" class="on" aria-current="page">', nav)
    assert current == [reverse(f"portal:{name}")]
    assert "btn" not in nav
