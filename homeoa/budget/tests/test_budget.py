import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse

from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import BillingError, default_schedule_kwargs, generate_assessments
from homeoa.budget import services
from homeoa.budget.models import Budget, BudgetLine, BudgetStatus
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.ledger.services import Line, post_entry
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership

D = Decimal
# Synthetic units with the demo association's allocated interests.
INTERESTS = {"A": "11.5556", "B": "11.5556", "C": "11.5556", "D": "10.6667", "E": "11.5556"}
INTERESTS |= {"F": "11.1111", "G": "10.2221", "H": "10.2221", "I": "11.5556"}


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Test Condo", short_name="TC", timezone="UTC")
    load_chart_of_accounts(a)
    with association_context(a):
        for number, share in INTERESTS.items():
            Unit.objects.create(association=a, number=number, allocated_interest=D(share))
        yield a


def acct(code):
    return Account.objects.get(code=code)


def fund(code):
    return Fund.objects.get(code=code)


def add(budget, group, label, code, amount, fund_code="operating", note=""):
    return BudgetLine.objects.create(
        budget=budget,
        group=group,
        label=label,
        account=acct(code),
        fund=fund(fund_code),
        amount=D(amount),
        note=note,
    )


def pilot_2026(assoc, adopt=True):
    b = services.create_budget(assoc, 2026, name="2026 budget")
    add(b, "Income", "Homeowner Assessment", "4000", "41188.49")
    add(b, "Transfers", "Transfer to Reserves", "5900", "7550.00")
    add(b, "Transfers", "Transfer to Insurance Reserves", "5910", "10717.00")
    add(b, "Maintenance", "General Repair & Maint.", "5200", "4000.00")
    if adopt:
        b = services.adopt(b)
    return b


def spend(code, amount, date, fund_code="operating"):
    post_entry(
        [
            Line(account=code, fund=fund_code, debit=D(amount)),
            Line(account="1000", fund=fund_code, credit=D(amount)),
        ],
        date=date,
        memo="test",
    )


# ------------------------------------------------------------------ adoption


def test_adopt_supersedes_and_one_adopted_per_year(assoc):
    first = pilot_2026(assoc)
    assert first.status == BudgetStatus.ADOPTED
    draft = services.create_budget(assoc, 2026, copy_from=first)
    assert draft.lines.count() == 4 and draft.is_draft
    services.adopt(draft)
    first.refresh_from_db()
    assert first.status == BudgetStatus.SUPERSEDED
    assert services.adopted_for(assoc, 2026).pk == draft.pk
    assert AuditEvent.objects.filter(action="budget.adopt").count() == 2
    last = AuditEvent.objects.filter(action="budget.adopt").order_by("-pk").first()
    assert last.changes["superseded"] == [first.pk]
    with pytest.raises(IntegrityError), transaction.atomic():
        Budget.objects.filter(pk=first.pk).update(status=BudgetStatus.ADOPTED)


def test_adopted_cannot_be_adopted_again_or_empty(assoc):
    b = pilot_2026(assoc)
    with pytest.raises(services.BudgetError):
        services.adopt(b)
    empty = services.create_budget(assoc, 2027)
    with pytest.raises(services.BudgetError):
        services.adopt(empty)


def test_compare_and_totals(assoc):
    adopted = pilot_2026(assoc)
    draft = services.create_budget(assoc, 2026, copy_from=adopted)
    line = draft.lines.get(label="General Repair & Maint.")
    line.amount = D("4480.00")
    line.note = "+12%"
    line.save()
    rows = {r.label: r for r in services.compare(adopted, draft)}
    row = rows["General Repair & Maint."]
    assert row.change == D("480.00") and row.change_pct == D("12.0") and row.note == "+12%"
    t = services.totals(adopted)
    assert t["income"] == D("41188.49")
    assert t["expense"] == D("22267.00")
    groups = services.grouped(adopted)
    assert [g.name for g in groups] == ["Income", "Transfers", "Maintenance"]
    assert groups[1].total == D("18267.00")


# ------------------------------------------------------------------ budget vs actual


def test_budget_vs_actual_figures(assoc):
    """Plan scenario: $46,800 budget, $33,212.40 spent YTD at 75% of the year."""
    b = services.create_budget(assoc, 2026)
    add(b, "Expenses", "Utilities", "5000", "20000.00")
    add(b, "Expenses", "Landscaping", "5100", "6800.00")
    add(b, "Expenses", "Repairs", "5200", "20000.00")
    services.adopt(b)
    spend("5000", "15000.00", dt.date(2026, 3, 1))
    spend("5100", "5212.40", dt.date(2026, 6, 1))
    spend("5200", "13000.00", dt.date(2026, 10, 7))
    spend("5200", "999.00", dt.date(2025, 12, 31))  # prior year: excluded
    spend("5200", "111.00", dt.date(2026, 10, 8))  # after the clock: excluded
    report = services.budget_vs_actual(b, dt.date(2026, 10, 7))
    assert report.expense_budget == D("46800.00")
    assert report.expense_actual == D("33212.40")
    assert report.elapsed_pct == D("75.0")
    assert report.expense_used_pct == D("71.0")
    rows = {r.account.code: r for _g, rs in report.groups for r in rs}
    assert rows["5100"].variance == D("-1587.60")
    assert rows["5100"].variance_pct == D("-23.3")
    assert rows["5000"].used_pct == D("75.0")


def test_budget_vs_actual_combines_lines_on_same_account(assoc):
    b = services.create_budget(assoc, 2026)
    add(b, "Service", "Water", "5000", "2083.00")
    add(b, "Service", "Garbage", "5000", "3100.00")
    spend("5000", "100.00", dt.date(2026, 2, 1))
    report = services.budget_vs_actual(b, dt.date(2026, 12, 31))
    (row,) = report.groups[0][1]
    assert row.budget == D("5183.00") and row.actual == D("100.00")
    assert report.months == 12


def test_income_actual_is_credit_normal(assoc):
    b = pilot_2026(assoc)
    post_entry(
        [
            Line(account="1100", fund="operating", debit=D("3432.36")),
            Line(account="4000", fund="operating", credit=D("3432.36")),
        ],
        date=dt.date(2026, 1, 1),
    )
    report = services.budget_vs_actual(b, dt.date(2026, 1, 15))
    assert report.income_actual == D("3432.36")
    assert report.months == 0


# ------------------------------------------------------------------ dues


def test_dues_plan_rounding(assoc):
    plan = services.plan_dues(pilot_2026(assoc))
    amounts = {u.number: a for u, a in plan.unit_amounts}
    assert amounts["A"] == D("396.63")
    assert amounts["D"] == D("366.12")
    assert amounts["F"] == D("381.37")
    assert amounts["G"] == D("350.86")
    assert plan.monthly_total == D("3432.36")
    assert plan.yearly_total == D("41188.32")
    assert plan.schedule.reserve_annual_amount == D("7550.00")
    assert plan.schedule.insurance_annual_amount == D("10717.00")


def test_draft_never_drives_dues(assoc):
    draft = pilot_2026(assoc, adopt=False)
    with pytest.raises(services.BudgetError):
        services.plan_dues(draft)
    with pytest.raises(services.BudgetError):
        services.schedule_from_budget(draft)
    assert not AssessmentSchedule.objects.exists()


def test_schedule_from_budget_creates_schedule(assoc):
    b = pilot_2026(assoc)
    s = services.schedule_from_budget(b)
    assert s.annual_amount == D("41188.49")
    assert s.effective_start == dt.date(2026, 1, 1)
    assert s.effective_end == dt.date(2026, 12, 31)
    assert AuditEvent.objects.filter(action="budget.dues_schedule").exists()


def test_existing_schedule_untouched_unless_confirmed(assoc):
    old = AssessmentSchedule.objects.create(
        association=assoc,
        name="Old dues",
        annual_amount=D("40000.00"),
        effective_start=dt.date(2025, 1, 1),
        **default_schedule_kwargs(assoc),
    )
    b = pilot_2026(assoc)
    with pytest.raises(services.BudgetError):
        services.schedule_from_budget(b)
    old.refresh_from_db()
    assert old.effective_end is None and AssessmentSchedule.objects.count() == 1
    services.schedule_from_budget(b, replace=True)
    old.refresh_from_db()
    assert old.effective_end == dt.date(2025, 12, 31)
    assert AssessmentSchedule.objects.count() == 2


def test_mid_year_replace_never_rebills(assoc):
    for unit in Unit.objects.all():
        person = Person.objects.create(association=assoc, full_name=f"Owner {unit.number}")
        set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
    old = AssessmentSchedule.objects.create(
        association=assoc,
        name="Old dues",
        annual_amount=D("40000.00"),
        effective_start=dt.date(2026, 1, 1),
        **default_schedule_kwargs(assoc),
    )
    for month in (1, 2, 3):
        generate_assessments(old, dt.date(2026, month, 1))
    b = pilot_2026(assoc)
    plan = services.plan_dues(b)
    assert plan.schedule.effective_start == dt.date(2026, 4, 1)
    new = services.schedule_from_budget(b, replace=True)
    old.refresh_from_db()
    assert old.active and old.effective_end == dt.date(2026, 3, 31)
    assert new.effective_start == dt.date(2026, 4, 1)
    generate_assessments(new, dt.date(2026, 4, 1))
    with pytest.raises(BillingError):
        generate_assessments(new, dt.date(2026, 3, 1))
    with pytest.raises(BillingError):
        generate_assessments(old, dt.date(2026, 4, 1))
    for unit in Unit.objects.all():
        for month in (1, 2, 3, 4):
            assert Charge.objects.filter(unit=unit, period=dt.date(2026, month, 1)).count() == 1


def test_replace_ends_partly_billed_later_schedule(assoc):
    for unit in Unit.objects.all():
        person = Person.objects.create(association=assoc, full_name=f"Owner {unit.number}")
        set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
    old = AssessmentSchedule.objects.create(
        association=assoc,
        name="Old dues",
        annual_amount=D("40000.00"),
        effective_start=dt.date(2026, 6, 1),
        **default_schedule_kwargs(assoc),
    )
    generate_assessments(old, dt.date(2026, 6, 1))
    b = pilot_2026(assoc)
    # The new schedule starts in July; the old one starts before it and is ended in June.
    services.schedule_from_budget(b, replace=True)
    old.refresh_from_db()
    assert old.active and old.effective_end == dt.date(2026, 6, 30)


def test_dues_preview_says_from_month(board_client, assoc):
    b = pilot_2026(assoc)
    r = board_client.get(reverse("budget:dues", args=[b.pk]))
    assert "from January 2026" in r.content.decode()


# ------------------------------------------------------------------ views


@pytest.fixture
def board_client(client, assoc):
    user = get_user_model().objects.create_user("treas@example.com", password="x")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


def test_views_render(board_client, assoc):
    b = pilot_2026(assoc)
    services.create_budget(assoc, 2026, copy_from=b)
    for url in (
        reverse("budget:index") + "?year=2026",
        reverse("budget:detail", args=[b.pk]),
        reverse("budget:actual", args=[b.pk]),
        reverse("budget:dues", args=[b.pk]),
        reverse("budget:new"),
    ):
        resp = board_client.get(url)
        assert resp.status_code == 200, url
    resp = board_client.get(reverse("budget:dues", args=[b.pk]))
    assert b"$3,432.36" in resp.content


def test_adopt_and_lines_via_views(board_client, assoc):
    b = services.create_budget(assoc, 2027)
    data = {
        "lines-TOTAL_FORMS": "1",
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        "lines-0-group": "Income",
        "lines-0-label": "Assessments",
        "lines-0-account": acct("4000").pk,
        "lines-0-fund": fund("operating").pk,
        "lines-0-amount": "46130.00",
        "lines-0-note": "+12%",
        "lines-0-sort_order": "0",
    }
    resp = board_client.post(reverse("budget:lines", args=[b.pk]), data)
    assert resp.status_code == 302
    assert b.lines.get().amount == D("46130.00")
    board_client.post(reverse("budget:adopt", args=[b.pk]))
    b.refresh_from_db()
    assert b.is_adopted
    resp = board_client.get(reverse("budget:lines", args=[b.pk]))
    assert resp.status_code == 302  # adopted budgets are read-only


def test_resident_forbidden(client, assoc):
    user = get_user_model().objects.create_user("owner@example.com", password="x")
    client.force_login(user)
    assert client.get(reverse("budget:index")).status_code == 403
