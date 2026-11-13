import datetime as dt
from decimal import Decimal

import pytest

from homeoa import rules
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AdjustmentKind, AssessmentSchedule, Charge, ChargeKind
from homeoa.billing.services import adjustments as adj
from homeoa.billing.services import default_schedule_kwargs, generate_assessments
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account
from homeoa.ledger.services import account_balance, trial_balance, unit_balance
from homeoa.payments import services as pay
from homeoa.payments.models import Allocation, ReceiptStatus
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership, transfer_ownership

D = Decimal
OCT = dt.date(2026, 10, 1)
NOV = dt.date(2026, 11, 1)
SHARES = {1: "8.60", 2: "9.68", 3: "8.60", 4: "9.68", 5: "8.60", 6: "9.68"}
SHARES |= {7: "9.68", 8: "8.60", 9: "8.60", 10: "9.68", 11: "8.60"}


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    load_chart_of_accounts(a)
    with association_context(a):
        for n, share in SHARES.items():
            unit = Unit.objects.create(association=a, number=str(n), allocated_interest=D(share))
            person = Person.objects.create(association=a, full_name=f"Owner {n}")
            set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
        yield a


@pytest.fixture
def schedule(assoc):
    return AssessmentSchedule.objects.create(
        association=assoc,
        name="Regular dues",
        annual_amount=D("46800.00"),
        effective_start=dt.date(2026, 1, 1),
        **default_schedule_kwargs(assoc),
    )


def unit(n):
    return Unit.objects.get(number=str(n))


def bal(n):
    return unit_balance(unit(n).pk)


def charge(u, amount, kind=ChargeKind.OTHER, date=OCT, desc="Charge"):
    return adj.manual_charge(u, D(amount), "test", desc, kind=kind, date=date)


def receipt(n, amount, date=dt.date(2026, 10, 5), **kw):
    return pay.record_receipt(
        unit=unit(n), amount=D(amount), received_date=date, method="check", **kw
    )


def assert_books_balance():
    assert trial_balance().difference == 0


def test_unit7_check_pays_october_dues(assoc, schedule):
    generate_assessments(schedule, OCT)
    assert bal(7) == D("377.52")
    r = receipt(7, "377.52", reference="1042")
    assert bal(7) == 0
    c = Charge.objects.get(unit=unit(7))
    assert c.open_balance == 0
    allocs = list(r.allocations.all())
    assert [(a.charge_id, a.amount) for a in allocs] == [(c.pk, D("377.52"))]
    assert account_balance(Account.objects.get(code="1000")) == D("377.52")
    assert r.entry.source_type == "payments.receipt"
    assert_books_balance()


def test_unit8_overpayment_is_50_credit_and_next_charge_consumes_it(assoc, schedule):
    generate_assessments(schedule, OCT)
    r = receipt(8, "385.40")
    assert bal(8) == D("-50.00")
    assert pay.unapplied(r) == D("50.00")
    assert account_balance(Account.objects.get(code="2100")) == D("50.00")
    generate_assessments(schedule, NOV)
    nov = Charge.objects.get(unit=unit(8), period=NOV)
    assert nov.open_balance == D("285.40")
    assert pay.unapplied(r) == 0
    assert bal(8) == D("285.40")
    assert account_balance(Account.objects.get(code="2100")) == 0
    assert_books_balance()


def test_partial_payment(assoc, schedule):
    generate_assessments(schedule, OCT)
    receipt(7, "100.00")
    assert Charge.objects.get(unit=unit(7)).open_balance == D("277.52")
    assert bal(7) == D("277.52")


def _three_charges():
    u = unit(3)
    old_fee = charge(u, "25.00", ChargeKind.LATE_FEE, dt.date(2026, 8, 1), "Late fee Aug")
    dues = charge(u, "335.40", ChargeKind.ASSESSMENT, dt.date(2026, 9, 1), "Dues Sep")
    other = charge(u, "40.00", ChargeKind.OTHER, dt.date(2026, 7, 1), "Damage")
    return u, old_fee, dues, other


def _applied(r):
    return [(a.charge.description, a.amount) for a in r.allocations.order_by("pk")]


def test_default_order_assessments_first(assoc):
    _u, *_ = _three_charges()
    r = receipt(3, "350.00")
    assert _applied(r) == [("Dues Sep", D("335.40")), ("Late fee Aug", D("14.60"))]


def test_fees_first_order(assoc):
    rules.set_override(
        assoc,
        "payments.allocation_order",
        ["late_fee", "interest", "assessment", "other"],
        reason="board vote",
    )
    _three_charges()
    r = receipt(3, "100.00")
    assert _applied(r) == [("Late fee Aug", D("25.00")), ("Dues Sep", D("75.00"))]


def test_oldest_first_mode_across_types(assoc):
    _u, fee, dues, other = _three_charges()
    ordered = pay.order_charges([fee, dues, other], ["assessment"], mode="oldest")
    assert [c.description for c in ordered] == ["Damage", "Late fee Aug", "Dues Sep"]
    newest = pay.order_charges([fee, dues, other], ["other"], oldest_first=False)
    assert newest[0].description == "Damage"


def test_oldest_first_within_type(assoc):
    u = unit(3)
    charge(u, "10.00", ChargeKind.ASSESSMENT, dt.date(2026, 9, 1), "Sep")
    charge(u, "10.00", ChargeKind.ASSESSMENT, dt.date(2026, 8, 1), "Aug")
    r = receipt(3, "10.00")
    assert _applied(r) == [("Aug", D("10.00"))]


def test_void_receipt_reverses_entry_and_allocations(assoc, schedule):
    generate_assessments(schedule, OCT)
    r = receipt(7, "377.52")
    pay.void_receipt(r, reason="Bounced")
    r.refresh_from_db()
    assert r.status == ReceiptStatus.VOID
    assert bal(7) == D("377.52")
    assert Charge.objects.get(unit=unit(7)).open_balance == D("377.52")
    assert not Allocation.objects.filter(receipt=r, released_entry__isnull=True).exists()
    assert account_balance(Account.objects.get(code="1000")) == 0
    assert AuditEvent.objects.filter(action="payments.receipt_void").exists()
    with pytest.raises(pay.PaymentError):
        pay.void_receipt(r, reason="again")
    assert_books_balance()


def test_void_receipt_after_credit_consumed(assoc, schedule):
    generate_assessments(schedule, OCT)
    r = receipt(8, "385.40")
    generate_assessments(schedule, NOV)
    pay.void_receipt(r, reason="Bounced", date=dt.date(2026, 11, 5))
    assert bal(8) == D("670.80")
    assert account_balance(Account.objects.get(code="2100")) == 0
    assert account_balance(Account.objects.get(code="1100")) == D("3900.00") * 2
    assert_books_balance()


def test_void_receipt_needs_reason(assoc, schedule):
    generate_assessments(schedule, OCT)
    r = receipt(7, "10.00")
    with pytest.raises(pay.PaymentError):
        pay.void_receipt(r, reason=" ")


def test_receipt_rejects_bad_amounts(assoc):
    for bad in (D("0"), D("-1.00"), D("1.005"), 5.0):
        with pytest.raises(pay.PaymentError):
            pay.record_receipt(unit=unit(1), amount=bad, received_date=OCT, method="check")


def test_receipt_stays_in_ownership_period(assoc, schedule):
    generate_assessments(schedule, OCT)
    u = unit(5)
    buyer = Person.objects.create(association=assoc, full_name="Buyer")
    transfer_ownership(u, [buyer], dt.date(2026, 10, 20))
    r = receipt(5, "100.00", date=dt.date(2026, 10, 25))
    # The buyer's payment doesn't pay the seller's October charge.
    assert pay.unapplied(r) == D("100.00")
    assert Charge.objects.get(unit=u).open_balance == D("335.40")


def test_refund_recorded_outside(assoc, schedule):
    generate_assessments(schedule, OCT)
    r = receipt(8, "385.40")
    with pytest.raises(pay.PaymentError):
        pay.record_refund(r, amount=D("60.00"), date=OCT, method="check", reason="x")
    with pytest.raises(pay.PaymentError):
        pay.record_refund(r, amount=D("50.00"), date=OCT, method="check", reason="")
    pay.record_refund(
        r, amount=D("50.00"), date=dt.date(2026, 10, 9), method="check", reason="Owner asked"
    )
    assert bal(8) == 0
    assert pay.unapplied(r) == 0
    assert account_balance(Account.objects.get(code="1000")) == D("335.40")
    with pytest.raises(pay.PaymentError):
        pay.void_receipt(r, reason="x")
    assert_books_balance()


def test_write_off(assoc, schedule):
    generate_assessments(schedule, OCT)
    a = adj.write_off(unit(7), D("77.52"), "Uncollectible")
    assert a.account.code == "5800"
    assert bal(7) == D("300.00")
    with pytest.raises(pay.PaymentError):
        adj.write_off(unit(7), D("400.00"), "too much")
    with pytest.raises(pay.PaymentError):
        adj.write_off(unit(7), D("1.00"), "")
    assert AuditEvent.objects.filter(object_type__icontains="adjustment").exists()
    assert_books_balance()


def test_credit_memo_with_excess_becomes_credit(assoc, schedule):
    generate_assessments(schedule, OCT)
    c = Charge.objects.get(unit=unit(1))
    a = adj.credit_memo(unit(1), D("400.00"), "Billing error", charge=c)
    assert a.kind == AdjustmentKind.CREDIT_MEMO
    assert bal(1) == D("-64.60")
    generate_assessments(schedule, NOV)
    assert Charge.objects.get(unit=unit(1), period=NOV).open_balance == D("270.80")
    assert_books_balance()


def test_manual_charge_consumes_credit(assoc):
    receipt(2, "50.00")
    assert bal(2) == D("-50.00")
    c = adj.manual_charge(unit(2), D("80.00"), "Window broken by guest", "Damage repair")
    assert c.open_balance == D("30.00")
    assert c.entry.lines.filter(account__code="4200").exists()
    assert bal(2) == D("30.00")
    assert_books_balance()


def test_void_charge_reallocates(assoc):
    u = unit(4)
    c1 = charge(u, "100.00", ChargeKind.ASSESSMENT, dt.date(2026, 9, 1), "Sep")
    c2 = charge(u, "100.00", ChargeKind.ASSESSMENT, dt.date(2026, 10, 1), "Oct")
    receipt(4, "100.00")
    c1.refresh_from_db()
    assert c1.open_balance == 0
    adj.void_charge(c1, "Billed in error", date=dt.date(2026, 10, 10))
    c2.refresh_from_db()
    assert c2.open_balance == 0
    assert bal(4) == 0
    with pytest.raises(pay.PaymentError):
        adj.void_charge(c1, "again")
    assert_books_balance()


def test_unit_credit_query(assoc):
    receipt(9, "20.00")
    assert pay.unit_credit(unit(9).pk, association=assoc) == D("20.00")


def test_imported_payments_settle_charges_fifo(assoc, schedule):
    """Imports post Dr 1000 / Cr 1100 with no Receipt; charges count as settled."""
    from homeoa.ledger.services import Line, post_entry

    generate_assessments(schedule, OCT)
    generate_assessments(schedule, NOV)
    u = unit(7)
    op = Charge.objects.filter(unit=u).first().ownership_period
    post_entry(
        [
            Line(account="1000", fund="operating", debit=D("400.00")),
            Line(
                account="1100",
                fund="operating",
                credit=D("400.00"),
                unit_id=u.pk,
                ownership_period_id=op.pk,
            ),
        ],
        date=dt.date(2026, 11, 3),
        source_type="imports.payment",
    )
    r = receipt(7, "355.04", date=dt.date(2026, 11, 4))
    octc = Charge.objects.get(unit=u, period=OCT)
    novc = Charge.objects.get(unit=u, period=NOV)
    assert octc.open_balance == 0
    assert novc.open_balance == 0
    assert _applied(r) == [("Monthly dues · November 2026", D("355.04"))]
    assert bal(7) == 0


def test_escrow_payoff_tagged_to_seller_after_transfer(assoc, schedule):
    """A receipt tagged to the seller's period, dated after transfer, pays the seller."""
    generate_assessments(schedule, OCT)
    u = unit(5)
    seller_charge = Charge.objects.get(unit=u)
    seller_period = seller_charge.ownership_period
    buyer = Person.objects.create(association=assoc, full_name="Buyer")
    transfer_ownership(u, [buyer], dt.date(2026, 10, 20))
    r = receipt(5, "335.40", date=dt.date(2026, 10, 25), ownership_period=seller_period)
    seller_charge.refresh_from_db()
    assert seller_charge.open_balance == 0
    assert pay.unapplied(r) == 0
    assert unit_balance(u.pk, ownership_period_id=seller_period.pk) == 0


def test_buyer_receipt_never_pays_seller_even_with_credit_later(assoc, schedule):
    generate_assessments(schedule, OCT)
    u = unit(5)
    buyer = Person.objects.create(association=assoc, full_name="Buyer")
    transfer_ownership(u, [buyer], dt.date(2026, 10, 20))
    r = receipt(5, "500.00", date=dt.date(2026, 10, 25))
    pay.apply_credit(u, date=dt.date(2026, 10, 26))
    assert Charge.objects.get(unit=u, period=OCT).open_balance == D("335.40")
    assert pay.unapplied(r) == D("500.00")


def test_board_receipt_above_balance_is_not_capped(assoc, schedule):
    """The no-prepayment cap applies to owners only: a board receipt may leave a credit."""
    generate_assessments(schedule, OCT)
    receipt(7, "1000.00")
    assert bal(7) == D("377.52") - D("1000.00")
    assert_books_balance()
