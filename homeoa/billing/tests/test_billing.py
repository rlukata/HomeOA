import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from hypothesis import given
from hypothesis import strategies as st

from homeoa.access.testing import grant, make_live
from homeoa.billing.models import AssessmentSchedule, BillingRun, Charge, Method
from homeoa.billing.services import (
    BillingError,
    allocate,
    default_schedule_kwargs,
    generate_assessments,
    monthly_share,
    parse_period,
    post_due_assessments,
    preview,
)
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.jobs import periodic_jobs, run_due
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import JournalEntry, JournalLine
from homeoa.ledger.services import ownership_balance, trial_balance, unit_balance
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership, transfer_ownership

D = Decimal
OCT = dt.date(2026, 10, 1)
# data.js: six units at 8.60%, five at 9.68%.
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


def schedule(assoc, **kw):
    fields = {
        "association": assoc,
        "name": "Regular dues",
        "annual_amount": D("46800.00"),
        "effective_start": dt.date(2026, 1, 1),
        **default_schedule_kwargs(assoc),
    }
    fields.update(kw)
    return AssessmentSchedule.objects.create(**fields)


# ------------------------------------------------------------------ amounts


def test_allocate_places_remainder_on_largest_shares():
    shares = allocate(D("100.00"), [("a", D(1)), ("b", D(1)), ("c", D(1))])
    assert shares == {"a": D("33.34"), "b": D("33.33"), "c": D("33.33")}
    # Largest exact share takes the cent, not the first key.
    shares = allocate(D("10.00"), [("a", D(1)), ("b", D(2)), ("c", D(3))])
    assert sum(shares.values()) == D("10.00")
    assert shares == {"a": D("1.67"), "b": D("3.33"), "c": D("5.00")}


def test_allocate_negative_leftover():
    # Half-up rounds every share up; the excess comes off the largest shares first.
    shares = allocate(
        D("0.03"), [("a", D(1)), ("b", D(1)), ("c", D(1)), ("d", D(1)), ("e", D(1)), ("f", D(1))]
    )
    assert sum(shares.values()) == D("0.03")
    assert list(shares.values()) == [
        D("0.00"),
        D("0.00"),
        D("0.00"),
        D("0.01"),
        D("0.01"),
        D("0.01"),
    ]


@given(
    total=st.integers(min_value=0, max_value=10_000_000).map(lambda c: D(c) / 100),
    weights=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=20),
)
def test_allocate_always_sums_exactly(total, weights):
    shares = allocate(total, [(i, D(w) / 100) for i, w in enumerate(weights)])
    assert sum(shares.values()) == total
    assert all(v == v.quantize(D("0.01")) and v >= 0 for v in shares.values())


def test_allocate_is_deterministic():
    w = [(i, D("8.60") if i % 2 else D("9.68")) for i in range(11)]
    assert allocate(D("3901.00"), w) == allocate(D("3901.00"), w)


def test_parse_period():
    assert parse_period("2026-10") == OCT
    assert parse_period("2026-10-15") == OCT
    with pytest.raises(BillingError):
        parse_period("Oct")


def test_october_sample_amounts(assoc):
    plan = preview(schedule(assoc), OCT)
    assert not plan.problems
    amounts = sorted(r.amount for r in plan.rows)
    assert amounts == [D("335.40")] * 6 + [D("377.52")] * 5
    assert plan.total == D("3900.00")


def test_flat_and_unit_type(assoc):
    flat = schedule(assoc, method=Method.FLAT, annual_amount=None, period_amount=D("1000.00"))
    plan = preview(flat, OCT)
    # Each unit pays its own rounded amount; the total is their sum, not the budget.
    assert {r.amount for r in plan.rows} == {D("90.91")}
    assert plan.total == D("1000.01")

    typed = schedule(
        assoc,
        method=Method.UNIT_TYPE,
        annual_amount=None,
        type_amounts={"small": "300.00", "large": "350.00"},
        unit_types={str(n): "small" if s == "8.60" else "large" for n, s in SHARES.items()},
    )
    plan = preview(typed, OCT)
    assert plan.total == D("300.00") * 6 + D("350.00") * 5
    del typed.unit_types["11"]
    assert preview(typed, OCT).problems


def test_interest_total_must_be_100(assoc):
    Unit.objects.filter(number="11").update(allocated_interest=D("8.00"))
    with pytest.raises(BillingError, match="100%"):
        generate_assessments(schedule(assoc), OCT)
    assert not JournalEntry.objects.exists()


# ------------------------------------------------------------------ posting


def test_post_creates_charges_and_entries(assoc):
    result = generate_assessments(schedule(assoc), OCT)
    assert result.created and result.charges_created == 11
    assert Charge.objects.count() == 11
    assert JournalEntry.objects.filter(source_type="billing.charge").count() == 11
    for c in Charge.objects.all():
        assert c.entry.source_type == "billing.charge"
        assert c.open_balance == c.amount
        assert c.charge_date == OCT
        assert unit_balance(c.unit_id) == c.amount
        assert ownership_balance(c.ownership_period_id) == c.amount
    tb = trial_balance()
    assert tb.difference == 0
    ar = JournalLine.objects.filter(account__code="1100")
    assert sum(ln.debit for ln in ar) == D("3900.00")
    assert all(ln.ownership_period_id and ln.unit_id for ln in ar)


def test_post_is_idempotent(assoc):
    s = schedule(assoc)
    generate_assessments(s, OCT)
    again = generate_assessments(s, dt.date(2026, 10, 20))
    assert not again.created and again.charges_created == 0
    assert Charge.objects.count() == 11
    assert BillingRun.objects.count() == 1
    assert JournalEntry.objects.count() == 11


def _sum(code, fund, side):
    lines = JournalLine.objects.filter(account__code=code, fund__code=fund)
    return sum((getattr(ln, side) for ln in lines), D(0))


def test_owners_billed_in_operating_and_reserve_transfer_once_per_run(assoc):
    s = schedule(assoc, reserve_annual_amount=D("6000.00"))
    result = generate_assessments(s, OCT)
    # Owner charges are operating only.
    assert not JournalLine.objects.filter(account__code="1100", fund__code="reserve").exists()
    assert _sum("1100", "operating", "debit") == D("3900.00")
    assert _sum("4000", "operating", "credit") == D("3900.00")
    # One interfund entry: 5900/2900 in operating, 1900/4900 in reserve.
    entry = result.run.reserve_entry
    assert entry.source_type == "billing.reserve"
    assert _sum("5900", "operating", "debit") == D("500.00")
    assert _sum("2900", "operating", "credit") == D("500.00")
    assert _sum("1900", "reserve", "debit") == D("500.00")
    assert _sum("4900", "reserve", "credit") == D("500.00")
    assert all(ln.unit_id is None for ln in entry.lines.all())
    assert trial_balance().difference == 0
    generate_assessments(s, OCT)
    assert JournalEntry.objects.filter(source_type="billing.reserve").count() == 1


def test_no_reserve_entry_when_zero(assoc):
    result = generate_assessments(schedule(assoc), OCT)
    assert result.run.reserve_entry is None


def test_monthly_share_last_fiscal_month_takes_remainder():
    annual = D("46801.00")
    months = [dt.date(2026, m, 1) for m in range(1, 13)]
    shares = [monthly_share(annual, m, 1) for m in months]
    assert shares[:11] == [D("3900.08")] * 11
    assert shares[11] == D("3900.12")
    assert sum(shares) == annual
    # Fiscal year starting July: June is the last month.
    july = [monthly_share(annual, m, 7) for m in months]
    assert july[5] == D("3900.12") and sum(july) == annual


def test_year_of_runs_dues_fixed_and_transfers_sum_exactly(assoc):
    s = schedule(assoc, annual_amount=D("46801.00"), reserve_annual_amount=D("1000.01"))
    for m in range(1, 13):
        generate_assessments(s, dt.date(2026, m, 1))
    # No remainder month for dues. 46801/12 x 8.60% = 335.41, x 9.68% = 377.53.
    assert {r.total for r in BillingRun.objects.all()} == {D("3900.11")}
    assert set(Charge.objects.values_list("amount", flat=True)) == {D("335.41"), D("377.53")}
    # The remainder rule stays for interfund transfers.
    assert sum(r.reserve_amount for r in BillingRun.objects.all()) == D("1000.01")
    assert _sum("4900", "reserve", "credit") == D("1000.01")


def test_charge_goes_to_owner_of_record_on_charge_date(assoc):
    unit = Unit.objects.get(number="3")
    buyer = Person.objects.create(association=assoc, full_name="Buyer")
    new_period = transfer_ownership(unit, [buyer], dt.date(2026, 9, 15))
    generate_assessments(schedule(assoc), OCT)
    assert Charge.objects.get(unit=unit).ownership_period == new_period


def test_transfer_blocked_over_posted_charges(assoc):
    generate_assessments(schedule(assoc), OCT)
    unit = Unit.objects.get(number="3")
    buyer = Person.objects.create(association=assoc, full_name="Buyer")
    with pytest.raises(ValidationError, match="2026-10-01"):
        transfer_ownership(unit, [buyer], OCT)
    with pytest.raises(ValidationError):
        transfer_ownership(unit, [buyer], dt.date(2026, 9, 20))
    transfer_ownership(unit, [buyer], dt.date(2026, 10, 2))


def test_missing_owner_blocks_posting(assoc):
    Unit.objects.create(association=assoc, number="12", allocated_interest=D(0))
    s = schedule(assoc, method=Method.FLAT)
    with pytest.raises(BillingError, match="12"):
        generate_assessments(s, OCT)
    assert not Charge.objects.exists()


def test_schedule_effective_dates(assoc):
    s = schedule(assoc, effective_start=dt.date(2026, 11, 1))
    with pytest.raises(BillingError):
        generate_assessments(s, OCT)


# ------------------------------------------------------------------ job


def _post_job(assoc, day):
    run_due(as_of=day, association=assoc, names=["billing.post_assessments"])


def test_scheduled_dues_skip_a_go_live_month_whose_due_date_is_before_go_live(assoc):
    schedule(assoc, post_day=1, due_day=1)
    make_live(assoc, since=dt.date(2026, 10, 15))
    for day in (dt.date(2026, 10, 15), dt.date(2026, 10, 20), dt.date(2026, 10, 31)):
        _post_job(assoc, day)
    assert not Charge.objects.exists()
    _post_job(assoc, dt.date(2026, 11, 1))
    dues = Charge.objects.all()
    assert dues.count() == 11
    assert {c.due_date for c in dues} == {dt.date(2026, 11, 1)}


def test_scheduled_dues_post_the_go_live_month_when_go_live_is_on_the_first(assoc):
    schedule(assoc, post_day=1, due_day=1)
    make_live(assoc, since=OCT)
    _post_job(assoc, OCT)
    assert {c.due_date for c in Charge.objects.all()} == {OCT}


def test_scheduled_dues_post_the_go_live_month_when_due_after_mid_month_go_live(assoc):
    schedule(assoc, post_day=1, due_day=20)
    make_live(assoc, since=dt.date(2026, 10, 15))
    _post_job(assoc, dt.date(2026, 10, 15))
    assert {c.due_date for c in Charge.objects.all()} == {dt.date(2026, 10, 20)}


def test_scheduled_dues_in_the_go_live_month_are_dated_on_go_live(assoc):
    go_live = dt.date(2026, 10, 15)
    schedule(assoc, post_day=1, due_day=20)
    make_live(assoc, since=go_live)
    _post_job(assoc, go_live)
    dues = list(Charge.objects.select_related("entry"))
    assert dues and {c.charge_date for c in dues} == {go_live}
    assert {c.entry.date for c in dues} == {go_live}
    assert {c.due_date for c in dues} == {dt.date(2026, 10, 20)}
    assert BillingRun.objects.get().charge_date == go_live


def test_board_can_still_post_a_month_due_before_go_live_by_hand(assoc):
    s = schedule(assoc, post_day=1, due_day=1)
    make_live(assoc, since=dt.date(2026, 10, 15))
    generate_assessments(s, OCT)
    assert Charge.objects.count() == 11


def test_periodic_job_registered_and_posts(assoc):
    assert "billing.post_assessments" in periodic_jobs()
    make_live(assoc)
    schedule(assoc, post_day=1)
    run_due(as_of=OCT, association=assoc, names=["billing.post_assessments"])
    assert Charge.objects.count() == 11
    assert BillingRun.objects.get().source == "scheduled"
    run_due(as_of=dt.date(2026, 10, 2), association=assoc, names=["billing.post_assessments"])
    assert Charge.objects.count() == 11


def test_job_waits_for_post_day(assoc):
    schedule(assoc, post_day=5)
    assert post_due_assessments(assoc, dt.date(2026, 10, 4)) == []
    assert [r.created for r in post_due_assessments(assoc, dt.date(2026, 10, 5))] == [True]


# ------------------------------------------------------------------ views


@pytest.fixture
def board(assoc, client):
    user = get_user_model().objects.create_user(email="board@example.org")
    # finance.manage comes with the Treasurer title.
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


def test_board_member_previews_and_proposes_dues(assoc, client):
    """Any board member can preview a dues run and propose posting it; it posts once
    another board member approves."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    s = schedule(assoc)
    treasurer = get_user_model().objects.create_user(email="t@example.org")
    grant(treasurer, association=assoc, title="treasurer")
    user = get_user_model().objects.create_user(email="member@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    resp = client.get(f"/money/assessments/run/?schedule={s.pk}&period=2026-10")
    assert resp.status_code == 200
    client.post("/money/assessments/post/", {"schedule": s.pk, "period": "2026-10"})
    change = ChangeRequest.objects.get()
    assert change.summary == "Post October 2026 dues" and not Charge.objects.exists()
    changes.approve(change, treasurer)
    assert Charge.objects.count() == 11


def test_views_preview_post_rerun(assoc, board):
    s = schedule(assoc)
    assert board.get("/money/assessments/").status_code == 200
    resp = board.get("/money/assessments/run/")
    assert b'type="month"' in resp.content
    resp = board.get(f"/money/assessments/run/?schedule={s.pk}&period=2026-10")
    assert b"$377.52" in resp.content and b"$3,900.00" in resp.content
    assert not Charge.objects.exists()

    resp = board.post(
        "/money/assessments/post/", {"schedule": s.pk, "period": "2026-10"}, follow=True
    )
    text = resp.content.decode()
    assert "Posted 11 charges for October 2026." in text
    assert text.count("$3,900.00") == 1
    assert "$335.40" in text

    resp = board.post(
        "/money/assessments/post/", {"schedule": s.pk, "period": "2026-10"}, follow=True
    )
    assert "already posted; 0 charges created" in resp.content.decode()
    assert Charge.objects.count() == 11


def _adopted_resolution(assoc):
    from homeoa.governance import services as gov

    r = gov.create_resolution(assoc, title="2026 dues")
    gov.adopt(r, adopted_on=dt.date(2025, 12, 10))
    return r


def test_schedule_form(assoc, board):
    resp = board.get("/money/assessments/schedules/new/")
    assert resp.status_code == 200
    k = default_schedule_kwargs(assoc)
    resp = board.post(
        "/money/assessments/schedules/new/",
        {
            "name": "Dues",
            "method": "allocated_interest",
            "annual_amount": "46800.00",
            "type_amounts": "{}",
            "unit_types": "{}",
            "reserve_annual_amount": "6000.00",
            "receivable_account": k["receivable_account"].pk,
            "income_account": k["income_account"].pk,
            "operating_fund": k["operating_fund"].pk,
            "reserve_fund": k["reserve_fund"].pk,
            "reserve_expense_account": k["reserve_expense_account"].pk,
            "due_to_reserve_account": k["due_to_reserve_account"].pk,
            "due_from_operating_account": k["due_from_operating_account"].pk,
            "reserve_income_account": k["reserve_income_account"].pk,
            "insurance_annual_amount": "10717.00",
            "insurance_fund": k["insurance_fund"].pk,
            "insurance_expense_account": k["insurance_expense_account"].pk,
            "due_to_insurance_account": k["due_to_insurance_account"].pk,
            "insurance_due_from_operating_account": k["insurance_due_from_operating_account"].pk,
            "insurance_income_account": k["insurance_income_account"].pk,
            "post_day": 1,
            "due_day": 10,
            "effective_start": "2026-01-01",
            "active": "on",
            "change_resolution": _adopted_resolution(assoc).pk,
        },
    )
    assert resp.status_code == 302, resp.content
    assert AssessmentSchedule.objects.get().reserve_annual_amount == D("6000.00")
    assert AssessmentSchedule.objects.get().insurance_annual_amount == D("10717.00")


def test_permissions(assoc, client):
    user = get_user_model().objects.create_user(email="x@example.org")
    client.force_login(user)
    assert client.get("/money/assessments/").status_code == 403
    assert client.post("/money/assessments/post/", {}).status_code == 403


# ------------------------------------------------------------------ pilot dues and insurance

# The demo association: units A-I, 2026 assessment $41,188.49, insurance $10,717.
PILOT = {"A": "11.5556", "B": "11.5556", "C": "11.5556", "D": "10.6667", "E": "11.5556"}
PILOT |= {"F": "11.1111", "G": "10.2221", "H": "10.2221", "I": "11.5556"}


@pytest.fixture
def pilot(db):
    a = Association.objects.create(name="Pilot", short_name="P", timezone="America/Los_Angeles")
    load_chart_of_accounts(a)
    with association_context(a):
        for n, share in PILOT.items():
            unit = Unit.objects.create(association=a, number=n, allocated_interest=D(share))
            person = Person.objects.create(association=a, full_name=f"Owner {n}")
            set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
        yield a


def pilot_schedule(a):
    return schedule(
        a,
        annual_amount=D("41188.49"),
        reserve_annual_amount=D("7550.00"),
        insurance_annual_amount=D("10717.00"),
    )


def test_pilot_dues_table(pilot):
    plan = preview(pilot_schedule(pilot), OCT)
    assert not plan.problems
    amounts = {r.unit.number: r.amount for r in plan.rows}
    expected = {u: D("396.63") for u in "ABCEI"}
    expected |= {"D": D("366.12"), "F": D("381.37"), "G": D("350.86"), "H": D("350.86")}
    assert amounts == expected
    assert plan.total == D("3432.36")


def test_pilot_dues_identical_every_month(pilot):
    s = pilot_schedule(pilot)
    for m in range(1, 13):
        generate_assessments(s, dt.date(2026, m, 1))
    assert {r.total for r in BillingRun.objects.all()} == {D("3432.36")}
    assert _sum("4000", "operating", "credit") == D("41188.32")
    assert (
        not JournalLine.objects.filter(account__code="1100")
        .exclude(fund__code="operating")
        .exists()
    )


def test_insurance_transfer_twelve_months_sum_exactly(pilot):
    s = pilot_schedule(pilot)
    for m in range(1, 13):
        generate_assessments(s, dt.date(2026, m, 1))
    runs = BillingRun.objects.order_by("period")
    assert [r.insurance_amount for r in runs][:11] == [D("893.08")] * 11
    assert runs.last().insurance_amount == D("893.12")
    assert sum(r.insurance_amount for r in runs) == D("10717.00")
    assert _sum("5910", "operating", "debit") == D("10717.00")
    assert _sum("2920", "operating", "credit") == D("10717.00")
    assert _sum("1920", "insurance", "debit") == D("10717.00")
    assert _sum("4910", "insurance", "credit") == D("10717.00")
    assert JournalEntry.objects.filter(source_type="billing.insurance").count() == 12
    # Trial balance balances in every fund.
    for fund in ("operating", "reserve", "insurance"):
        lines = JournalLine.objects.filter(fund__code=fund)
        assert sum((ln.debit - ln.credit for ln in lines), D(0)) == 0


def test_insurance_transfer_idempotent_and_audited(pilot):
    from homeoa.audit.models import AuditEvent

    s = pilot_schedule(pilot)
    first = generate_assessments(s, OCT)
    again = generate_assessments(s, OCT)
    assert not again.created and again.run.pk == first.run.pk
    assert JournalEntry.objects.filter(source_type="billing.insurance").count() == 1
    entry = first.run.insurance_entry
    assert AuditEvent.objects.filter(object_id=str(entry.pk)).exists()


def test_insurance_needs_accounts(pilot):
    s = pilot_schedule(pilot)
    s.insurance_fund = None
    with pytest.raises(ValidationError):
        s.full_clean()
