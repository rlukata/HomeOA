import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa import rules
from homeoa.access.testing import grant, make_live
from homeoa.billing.models import (
    AssessmentSchedule,
    Charge,
    ChargeKind,
    LateFee,
    LateFeePolicy,
    SpecialAssessment,
    SpecialInstallment,
)
from homeoa.billing.services import (
    BillingError,
    default_schedule_kwargs,
    generate_assessments,
    latefees,
    special,
)
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.jobs import periodic_jobs, run_due
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import JournalEntry
from homeoa.ledger.services import trial_balance, unit_balance
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership

D = Decimal
SEP = dt.date(2026, 9, 1)
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
def september(assoc):
    """September dues posted; every unit but 7 paid (open balance cleared)."""
    s = AssessmentSchedule.objects.create(
        association=assoc,
        name="Dues",
        annual_amount=D("46800.00"),
        effective_start=dt.date(2026, 1, 1),
        **default_schedule_kwargs(assoc),
    )
    generate_assessments(s, SEP)
    for c in Charge.objects.exclude(unit__number="7"):
        pay(c.unit, c.amount, dt.date(2026, 9, 5))
    return Charge.objects.get(unit__number="7")


def pay(unit, amount, on):
    from homeoa.payments.services import record_receipt

    return record_receipt(unit=unit, amount=D(amount), received_date=on, method="check")


def policy(assoc, **kw):
    fields = {
        "association": assoc,
        "effective_start": dt.date(2026, 1, 1),
        **latefees.default_policy_kwargs(assoc),
    }
    fields.update(kw)
    return LateFeePolicy.objects.create(**fields)


# ------------------------------------------------------------------ late fees


def test_default_policy_uses_placeholders(assoc):
    p = policy(assoc)
    assert (p.flat_amount, p.grace_days, p.interest_apr) == (D("25.00"), 10, D("0"))
    assert not rules.resolve(assoc, "late_fees.amount").verified


def test_grace_boundary_and_unit_7_fee(assoc, september):
    p = policy(assoc)
    assert latefees.assess(p, dt.date(2026, 9, 10)).count == 0
    result = latefees.assess(p, dt.date(2026, 9, 11))
    assert result.count == 1
    fee = result.fees[0]
    assert fee.charge.unit.number == "7"
    assert fee.charge.amount == D("25.00")
    assert fee.charge.kind == ChargeKind.LATE_FEE
    assert fee.charge.ownership_period_id == september.ownership_period_id
    assert unit_balance(september.unit) == D("377.52") + D("25.00")
    assert trial_balance(association=assoc).difference == 0


def test_rerun_is_idempotent(assoc, september):
    p = policy(assoc)
    latefees.assess(p, dt.date(2026, 9, 11))
    assert latefees.assess(p, dt.date(2026, 9, 11)).count == 0
    assert latefees.assess(p, dt.date(2026, 9, 20)).count == 0
    assert LateFee.objects.count() == 1
    assert JournalEntry.objects.filter(source_type="billing.late_fee").count() == 1


def test_paid_units_and_minimum_balance(assoc, september):
    p = policy(assoc, minimum_balance=D("400.00"))
    assert latefees.assess(p, dt.date(2026, 9, 11)).count == 0
    p.minimum_balance = D("0")
    p.save()
    assert latefees.assess(p, dt.date(2026, 9, 11)).count == 1


def test_fees_not_charged_on_fees_or_before_effective_start(assoc, september):
    p = policy(assoc, effective_start=dt.date(2026, 10, 1))
    assert latefees.assess(p, dt.date(2026, 10, 15)).count == 0
    p2 = policy(assoc, name="Other")
    latefees.assess(p2, dt.date(2026, 9, 11))
    # The late fee itself is not an "assessment"; no fee on the fee.
    assert latefees.assess(p2, dt.date(2026, 10, 30)).count == 0


def test_charges_due_before_the_policy_start_get_no_fee_or_interest(assoc, september):
    """Loaded arrears due before the policy starts get nothing, even run by hand."""
    later = AssessmentSchedule.objects.create(
        association=assoc,
        name="Later dues",
        annual_amount=D("46800.00"),
        effective_start=dt.date(2026, 10, 1),
        **default_schedule_kwargs(assoc),
    )
    generate_assessments(later, dt.date(2026, 10, 1))
    newer = Charge.objects.get(unit__number="7", due_date=dt.date(2026, 10, 1))
    p = policy(assoc, effective_start=dt.date(2026, 10, 1), interest_apr=D("12"))
    for on in (dt.date(2026, 10, 11), dt.date(2026, 11, 1)):
        latefees.assess(p, on)
    old_fees = LateFee.objects.filter(source_charge=september)
    assert not old_fees.exists()
    kinds = set(LateFee.objects.filter(source_charge=newer).values_list("kind", flat=True))
    assert kinds == {"late_fee", "interest"}


def test_percent_and_cap(assoc, september):
    assert latefees.fee_amount(policy(assoc, percent=D("10")), D("377.52"), None) == (
        D("62.75"),
        D("62.75"),
        False,
    )
    rules.set_override(assoc, "late_fees.cap", "30.00", reason="test")
    p = policy(assoc, name="Pct", percent=D("10"))
    fee = latefees.assess(p, dt.date(2026, 9, 11)).fees[0]
    assert fee.charge.amount == D("30.00")
    assert fee.computed == D("62.75") and fee.capped


def test_interest_math():
    assert latefees.interest_amount(D("1000.00"), D("12"), 30) == D("9.86")
    assert latefees.interest_amount(D("377.52"), D("12"), 20) == D("2.48")
    assert latefees.interest_amount(D("377.52"), D("0"), 20) == D("0")
    assert latefees.interest_amount(D("377.52"), D("12"), 0) == D("0")


def test_interest_accrues_monthly_once(assoc, september):
    p = policy(assoc, flat_amount=D("0"), interest_apr=D("12"))
    # Sep 11..30 overdue = 20 days, posted on Oct 1.
    assert latefees.assess(p, dt.date(2026, 9, 30)).count == 0
    fees = latefees.assess(p, dt.date(2026, 10, 1)).fees
    assert [f.charge.amount for f in fees] == [D("2.48")]
    assert fees[0].period == SEP and fees[0].charge.kind == ChargeKind.INTEREST
    assert latefees.assess(p, dt.date(2026, 10, 5)).count == 0
    # Full October (31 days) on Nov 1.
    fees = latefees.assess(p, dt.date(2026, 11, 1)).fees
    assert [f.charge.amount for f in fees] == [D("3.85")]


def test_waiver_reverses_fee(assoc, september):
    p = policy(assoc)
    fee = latefees.assess(p, dt.date(2026, 9, 11)).fees[0]
    with pytest.raises(BillingError):
        latefees.waive(fee, reason=" ")
    latefees.waive(fee, reason="First time", on=dt.date(2026, 9, 12))
    fee.refresh_from_db()
    assert fee.waived and fee.waive_entry.reversal_of_id == fee.charge.entry_id
    fee.charge.refresh_from_db()
    assert fee.charge.void_entry_id == fee.waive_entry_id
    assert fee.charge.open_balance == D("0")
    from homeoa.billing.models import Adjustment

    assert Adjustment.objects.get(charge=fee.charge).kind == "charge_void"
    assert unit_balance(september.unit) == D("377.52")
    with pytest.raises(BillingError):
        latefees.waive(fee, reason="again")
    # No new fee after the waiver either.
    assert latefees.assess(p, dt.date(2026, 9, 13)).count == 0


def test_waiver_refused_after_payment_applied(assoc, september):
    fee = latefees.assess(policy(assoc), dt.date(2026, 9, 11)).fees[0]
    # By type: the dues are paid first, then $10 of the fee.
    pay(september.unit, D("387.52"), dt.date(2026, 9, 15))
    fee.charge.refresh_from_db()
    assert fee.charge.open_balance == D("15.00")
    with pytest.raises(BillingError):
        latefees.waive(fee, reason="x")
    fee.refresh_from_db()
    assert not fee.waived and fee.charge.void_entry_id is None


def test_allocation_mode_oldest_first(assoc, september):
    """payments.allocation_mode=oldest_first pays strictly by date across types."""
    from homeoa.payments.services import allocation_plan

    fee = latefees.assess(policy(assoc), dt.date(2026, 9, 11)).fees[0]
    older_fee = fee.charge
    # In memory only: make the fee older than the dues so the two modes differ.
    older_fee.charge_date = older_fee.due_date = dt.date(2026, 8, 1)
    assert rules.value(assoc, "payments.allocation_mode") == "by_type"
    plan = allocation_plan(assoc, [september, older_fee], D("25.00"))
    assert [c.pk for c, _ in plan] == [september.pk]
    rules.set_override(assoc, "payments.allocation_mode", "oldest_first", reason="test")
    assert not rules.resolve(assoc, "payments.allocation_mode").verified
    plan = allocation_plan(assoc, [september, older_fee], D("25.00"))
    assert [c.pk for c, _ in plan] == [older_fee.pk]


def test_periodic_job(assoc, september):
    assert "billing.assess_late_fees" in periodic_jobs()
    make_live(assoc)
    policy(assoc)
    run_due(as_of=dt.date(2026, 9, 11), association=assoc, names=["billing.assess_late_fees"])
    run_due(as_of=dt.date(2026, 9, 11), association=assoc, names=["billing.assess_late_fees"])
    assert LateFee.objects.count() == 1


# ------------------------------------------------------------------ special


def make_special(assoc, total="10000.00", n=4, **kw):
    kwargs = special.default_special_kwargs(assoc) | kw
    return special.create_special(
        assoc,
        name="Roof",
        total=D(total),
        installments=special.monthly_dates(dt.date(2026, 11, 1), n),
        **kwargs,
    )


def test_installment_split_and_remainder():
    assert special.split_installments(D("10000.00"), 4) == [D("2500.00")] * 4
    assert special.split_installments(D("100.00"), 3) == [D("33.34"), D("33.33"), D("33.33")]
    assert special.split_installments(D("0.05"), 3) == [D("0.01"), D("0.02"), D("0.02")]


def test_monthly_dates_cross_year():
    dates = special.monthly_dates(dt.date(2026, 11, 30), 3)
    assert [d for d, _ in dates] == [
        dt.date(2026, 11, 28),
        dt.date(2026, 12, 28),
        dt.date(2027, 1, 28),
    ]


def test_special_10000_in_4_by_interest_totals_exact(assoc):
    sa = make_special(assoc)
    assert sa.operating_fund.code == "operating" and not sa.for_reserves
    for inst in sa.installments.all():
        assert special.post_installment(inst)
    charges = Charge.objects.filter(kind=ChargeKind.SPECIAL)
    assert charges.count() == 44
    assert sum(c.amount for c in charges) == D("10000.00")
    per_unit = special.preview(sa)
    assert sum(r["total"] for r in per_unit.values()) == D("10000.00")
    unit7 = next(r for r in per_unit.values() if r["unit"].number == "7")
    assert unit7["total"] == D("968.00")
    assert trial_balance(association=assoc).difference == 0


def _fund_sum(code, fund, side):
    from django.db.models import Sum

    from homeoa.ledger.models import JournalLine

    qs = JournalLine.objects.filter(account__code=code, fund__code=fund)
    return qs.aggregate(s=Sum(side))["s"] or D("0")


def test_special_billed_in_operating_without_reserve_transfer(assoc):
    sa = make_special(assoc, n=2)
    for inst in sa.installments.all():
        special.post_installment(inst)
    assert _fund_sum("1100", "operating", "debit") == D("10000.00")
    assert not JournalEntry.objects.filter(source_type="billing.special_reserve").exists()
    assert not SpecialInstallment.objects.filter(reserve_entry__isnull=False).exists()
    from homeoa.ledger.models import JournalLine

    assert not JournalLine.objects.filter(fund__is_reserve=True).exists()


def test_special_for_reserves_posts_interfund_per_installment(assoc):
    sa = make_special(assoc, n=4, for_reserves=True)
    insts = list(sa.installments.all())
    special.post_installment(insts[0])
    assert special.post_installment(insts[0]) is False
    insts[0].refresh_from_db()
    entry = insts[0].reserve_entry
    assert entry.source_type == "billing.special_reserve"
    reserve = sa.operating_fund.__class__.objects.get(is_reserve=True)
    assert _fund_sum("1100", "operating", "debit") == D("2500.00")
    assert _fund_sum("5900", "operating", "debit") == D("2500.00")
    assert _fund_sum("2900", "operating", "credit") == D("2500.00")
    assert _fund_sum("1900", reserve.code, "debit") == D("2500.00")
    assert _fund_sum("4900", reserve.code, "credit") == D("2500.00")
    assert JournalEntry.objects.filter(source_type="billing.special_reserve").count() == 1
    for inst in insts[1:]:
        special.post_installment(inst)
    assert _fund_sum("4900", reserve.code, "credit") == D("10000.00")
    assert trial_balance(association=assoc).difference == 0


def test_special_remainder_cents(assoc):
    sa = make_special(assoc, total="1000.01", n=3)
    for inst in sa.installments.all():
        special.post_installment(inst)
    assert sum(c.amount for c in Charge.objects.filter(kind=ChargeKind.SPECIAL)) == D("1000.01")


def test_special_flat_and_idempotent(assoc):
    sa = make_special(assoc, total="1100.00", n=1, method="flat")
    inst = sa.installments.get()
    assert special.post_installment(inst) is True
    assert special.post_installment(inst) is False
    amounts = set(Charge.objects.filter(kind=ChargeKind.SPECIAL).values_list("amount", flat=True))
    assert amounts == {D("100.00")}


def test_special_job_posts_due_installments(assoc):
    assert "billing.post_special_installments" in periodic_jobs()
    make_live(assoc)
    make_special(assoc)
    names = ["billing.post_special_installments"]
    run_due(as_of=dt.date(2026, 10, 31), association=assoc, names=names)
    assert not SpecialInstallment.objects.filter(posted_at__isnull=False).exists()
    run_due(as_of=dt.date(2026, 12, 1), association=assoc, names=names)
    assert SpecialInstallment.objects.filter(posted_at__isnull=False).count() == 2


def test_scheduled_late_fees_skip_charges_due_before_go_live(assoc, september):
    policy(assoc)
    make_live(assoc, since=dt.date(2026, 10, 1))
    names = ["billing.assess_late_fees"]
    run_due(as_of=dt.date(2026, 10, 15), association=assoc, names=names)
    assert LateFee.objects.count() == 0
    # The board can still assess them by hand.
    assert latefees.assess_due(assoc, dt.date(2026, 10, 15))


def test_scheduled_special_installments_dated_before_go_live_are_not_posted(assoc):
    make_special(assoc)  # installments Nov 1, Dec 1, Jan 1, Feb 1
    make_live(assoc, since=dt.date(2026, 12, 1))
    names = ["billing.post_special_installments"]
    run_due(as_of=dt.date(2026, 12, 1), association=assoc, names=names)
    posted = SpecialInstallment.objects.filter(posted_at__isnull=False)
    assert list(posted.values_list("charge_date", flat=True)) == [dt.date(2026, 12, 1)]


def test_scheduled_interest_is_dated_on_or_after_go_live_and_counts_only_later_days(
    assoc, september
):
    go_live = dt.date(2026, 10, 15)
    october = AssessmentSchedule.objects.create(
        association=assoc,
        name="Dues from go-live",
        annual_amount=D("46800.00"),
        effective_start=dt.date(2026, 10, 1),
        due_day=20,
        **default_schedule_kwargs(assoc),
    )
    generate_assessments(october, dt.date(2026, 10, 1))
    for c in Charge.objects.filter(due_date=dt.date(2026, 10, 20)).exclude(unit__number="7"):
        pay(c.unit, c.amount, dt.date(2026, 10, 18))
    owed = Charge.objects.get(unit__number="7", due_date=dt.date(2026, 10, 20))
    policy(assoc, flat_amount=D("0"), interest_apr=D("12"))
    make_live(assoc, since=go_live)
    run_due(as_of=dt.date(2026, 11, 1), association=assoc, names=["billing.assess_late_fees"])
    fees = list(LateFee.objects.all())
    # Only the charge due after go-live accrues: Oct 30-31 (10-day grace), 2 days.
    assert [f.source_charge_id for f in fees] == [owed.pk]
    assert fees[0].charge.amount == latefees.interest_amount(owed.amount, D("12"), 2)
    assert fees[0].charge.charge_date >= go_live
    assert fees[0].charge.entry.date >= go_live


def test_special_late_fee_applies(assoc):
    sa = make_special(assoc, n=1)
    special.post_installment(sa.installments.get())
    p = policy(assoc)
    assert latefees.assess(p, dt.date(2026, 11, 11)).count == 11


# ------------------------------------------------------------------ views


@pytest.fixture
def board(assoc, client):
    user = get_user_model().objects.create_user(email="board@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


def test_views_late_fees_and_waive(assoc, september, board):
    resp = board.get("/money/assessments/late-fees/")
    assert resp.status_code == 200
    assert b"Unverified" in resp.content or b"Verified" in resp.content
    resp = board.post(
        "/money/assessments/late-fees/policies/new/",
        {
            "name": "Late fees",
            "active": "on",
            "grace_days": "10",
            "flat_amount": "25.00",
            "percent": "0",
            "interest_apr": "0",
            "minimum_balance": "0",
            "applies_to": '["assessment"]',
            "effective_start": "2026-01-01",
            "receivable_account": latefees.default_policy_kwargs(assoc)["receivable_account"].pk,
            "fee_income_account": latefees.default_policy_kwargs(assoc)["fee_income_account"].pk,
            "interest_income_account": latefees.default_policy_kwargs(assoc)[
                "interest_income_account"
            ].pk,
            "fund": latefees.default_policy_kwargs(assoc)["fund"].pk,
        },
    )
    assert resp.status_code == 302, resp.content.decode()[:2000]
    fee = latefees.assess(LateFeePolicy.objects.get(), dt.date(2026, 9, 11)).fees[0]
    resp = board.get("/money/assessments/late-fees/")
    assert b"$25.00" in resp.content
    resp = board.post(f"/money/assessments/late-fees/{fee.pk}/waive/", {"reason": "Courtesy"})
    assert resp.status_code == 302
    fee.refresh_from_db()
    assert fee.waived


def _resolution(assoc, title="Roof special assessment", adopt=True):
    from homeoa.governance import services as gov

    r = gov.create_resolution(assoc, title=title)
    if adopt:
        gov.adopt(r, adopted_on=dt.date(2026, 10, 1))
    return r


def _special_form(assoc, resolution=None):
    d = special.default_special_kwargs(assoc)
    data = {
        "name": "Roof",
        "total": "10000.00",
        "method": "allocated_interest",
        "installments": "4",
        "first_due": "2026-11-01",
        "operating_fund": d["operating_fund"].pk,
        "for_reserves": "on",
        "receivable_account": d["receivable_account"].pk,
        "income_account": d["income_account"].pk,
    }
    if resolution is not None:
        data["change_resolution"] = resolution.pk
    return data


def test_views_special(assoc, board):
    resp = board.post("/money/assessments/special/new/", _special_form(assoc, _resolution(assoc)))
    assert resp.status_code == 302
    detail = resp["Location"]
    resp = board.get(detail)
    assert b"$968.00" in resp.content and b"$2,500.00" in resp.content
    inst = SpecialInstallment.objects.get(number=1)
    resp = board.post(f"/money/assessments/special/installments/{inst.pk}/post/")
    assert resp.status_code == 302
    assert Charge.objects.filter(kind=ChargeKind.SPECIAL).count() == 11
    assert board.get("/money/assessments/special/").status_code == 200


def test_write_views_need_finance_access(assoc, client):
    """Owners never reach the money screens; board members do, to propose changes."""
    user = get_user_model().objects.create_user(email="o@example.org")
    client.force_login(user)
    assert client.get("/money/assessments/late-fees/").status_code == 403
    assert client.get("/money/assessments/late-fees/policies/new/").status_code == 403
    assert client.post("/money/assessments/late-fees/1/waive/", {"reason": "x"}).status_code == 403
    assert client.get("/money/assessments/special/new/").status_code == 403
    assert client.post("/money/assessments/special/installments/1/post/").status_code == 403
    member = get_user_model().objects.create_user(email="m@example.org")
    grant(member, association=assoc)
    client.force_login(member)
    assert client.get("/money/assessments/special/new/").status_code == 200


def test_special_assessment_needs_a_board_resolution(assoc, board):
    """A special assessment is approved by a board resolution: none chosen, nothing saved;
    a draft resolution holds it until adopted."""
    from homeoa.changes.models import ChangeRequest
    from homeoa.governance import services as gov

    resp = board.post("/money/assessments/special/new/", _special_form(assoc))
    assert resp.status_code == 200 and not SpecialAssessment.objects.exists()
    draft = _resolution(assoc, adopt=False)
    resp = board.post("/money/assessments/special/new/", _special_form(assoc, draft))
    change = ChangeRequest.objects.get()
    assert change.resolution == draft and change.is_pending
    assert not SpecialAssessment.objects.exists()
    gov.adopt(draft, adopted_on=dt.date(2026, 10, 2))
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.APPLIED, change.error
    assert SpecialAssessment.objects.get().name == "Roof"
    # One resolution backs one change.
    resp = board.post("/money/assessments/special/new/", _special_form(assoc, draft))
    assert resp.status_code == 200 and SpecialAssessment.objects.count() == 1


def test_special_assessment_waits_for_owner_ratification(assoc):
    """Where the rule pack requires it (WA: RCW 64.90.525(3)), nothing posts until the
    owners ratify, and the meeting must fall in the budget's ratification window."""
    from homeoa.billing.services import BillingError

    rules.set_override(assoc, special.RATIFICATION_KEY, True, reason="WA")
    rules.set_override(assoc, "budget.ratification.min_days", 14, reason="WA")
    rules.set_override(assoc, "budget.ratification.max_days", 50, reason="WA")
    sa = make_special(assoc)
    inst = sa.installments.first()
    with pytest.raises(BillingError, match="not ratified"):
        special.post_installment(inst)
    assert special.post_due_installments(assoc, dt.date(2027, 6, 1)) == 0
    sent = dt.date(2026, 10, 1)
    with pytest.raises(BillingError, match="between"):
        special.record_ratification(
            sa, summary_sent_on=sent, meeting_on=sent + dt.timedelta(days=7), outcome="ratified"
        )
    special.record_ratification(
        sa, summary_sent_on=sent, meeting_on=sent + dt.timedelta(days=20), outcome="ratified"
    )
    assert special.post_installment(inst) is True


def test_emergency_vote_makes_it_effective_and_tells_owners(
    assoc, django_capture_on_commit_callbacks
):
    """WA (RCW 64.90.502(6)): in an emergency a two-thirds board vote makes a special
    assessment effective without owner ratification; the owners are told right away."""
    from homeoa.billing.services import BillingError

    rules.set_override(assoc, special.RATIFICATION_KEY, True, reason="WA")
    sa = make_special(assoc)
    with pytest.raises(BillingError, match="does not allow"):
        _emergency(sa, votes_for=3, seats=3)
    rules.set_override(assoc, special.EMERGENCY_KEY, "two_thirds", reason="WA")
    with pytest.raises(BillingError, match="two-thirds"):
        _emergency(sa, votes_for=1, seats=3)
    with pytest.raises(BillingError, match="kind of emergency"):
        _emergency(sa, votes_for=2, seats=3, reason=" ")
    with django_capture_on_commit_callbacks(execute=True):
        _emergency(sa, votes_for=2, seats=3)
    sa.refresh_from_db()
    assert sa.ratification == "emergency" and special.may_post(sa)
    assert special.post_installment(sa.installments.first()) is True


def _emergency(sa, *, votes_for, seats, reason="Storm tore off the roof over units B and C"):
    return special.record_emergency(
        sa,
        kind="habitability",
        reason=reason,
        decided_on=dt.date(2026, 10, 5),
        votes_for=votes_for,
        seats=seats,
    )


def test_board_vote_thresholds():
    assert special.vote_passes("two_thirds", 2, 3) and not special.vote_passes("two_thirds", 3, 5)
    assert special.vote_passes("two_thirds", 4, 6) and not special.vote_passes("majority", 2, 4)
    assert special.vote_passes("unanimous", 5, 5) and not special.vote_passes("majority", 4, 3)


def test_rejected_special_assessment_never_posts(assoc):
    from homeoa.billing.services import BillingError

    sa = make_special(assoc)
    special.record_ratification(
        sa, summary_sent_on=dt.date(2026, 10, 1), meeting_on=dt.date(2026, 10, 20),
        outcome="rejected",
    )  # fmt: skip
    with pytest.raises(BillingError):
        special.post_installment(sa.installments.first())


def test_special_income_defaults_to_4050_even_on_old_charts(assoc):
    from homeoa.ledger.models import Account

    assert special.default_special_kwargs(assoc)["income_account"].code == "4050"
    Account.objects.filter(code="4050").delete()
    acct = special.default_special_kwargs(assoc)["income_account"]
    assert acct.code == "4050" and acct.type == "income"


def test_waiving_and_posting_installments_need_a_post(assoc, september, board):
    """A plain link (GET) never waives a fee or posts an installment."""
    from django.urls import reverse

    assert board.get(reverse("billing:late_fee_waive", args=[1])).status_code == 405
    assert board.get(reverse("billing:installment_post", args=[1])).status_code == 405
