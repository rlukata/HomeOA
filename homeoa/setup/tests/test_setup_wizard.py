"""Association setup wizard: answers become a draft resolution, and adopting it
applies the policies."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers, rules
from homeoa.access.testing import grant, make_live
from homeoa.ap.approvals.models import ApprovalRule
from homeoa.billing.latefee_models import LateFeePolicy
from homeoa.changes.models import ChangeRequest
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.enforcement.models import FineSchedule
from homeoa.governance import services as gov
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.maintenance import oneoff
from homeoa.plugins import registry
from homeoa.plugins import services as plugin_services
from homeoa.setup import services, targets

D = Decimal
ADOPTED = dt.date(2026, 11, 12)


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def wa(assoc):
    registry.register(wa_manifest)
    plugin_services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    return assoc


@pytest.fixture
def treasurer(assoc):
    user = get_user_model().objects.create_user("t@example.org")
    grant(user, association=assoc, title="treasurer")
    return user


def suggested(s):
    """POST data choosing every suggested answer."""
    data = {}
    for i, q in enumerate(s.questions):
        data[services.field_name(i)] = str(q.suggested)
    return data


def adopt(change):
    gov.adopt(change.resolution, adopted_on=ADOPTED)
    change.refresh_from_db()
    return change


def test_washington_setup_suggests_values_with_citations(wa):
    sections = {s.key: s for s in services.sections(wa)}
    assert list(sections) == ["features", "money", "enforcement", "approvals", "records"]
    late_fee = sections["money"].questions[0]
    assert late_fee.suggested == D("15")
    assert late_fee.citation == "RCW 64.34.364(17)(b)"
    reminder = next(q for q in sections["money"].questions if q.target.endswith("reminder_days"))
    assert reminder.suggested is None and reminder.attorney_review


def test_answers_draft_a_resolution_and_change_nothing_until_adopted(wa, treasurer):
    s = services.section(wa, "money")
    answers, errors = services.parse(s, suggested(s))
    assert errors == {}
    change = services.propose(wa, s, answers, user=treasurer)
    resolution = change.resolution
    assert resolution.is_draft
    assert "Late fee for an assessment paid late: $15.00. (See RCW 64.34.364(17)(b).)" in (
        resolution.text
    )
    assert not LateFeePolicy.objects.exists()
    assert services.state(wa, s).pending == change
    with pytest.raises(services.SetupError):
        services.propose(wa, s, answers, user=treasurer)


def test_adopting_money_policies_applies_them(wa, treasurer):
    s = services.section(wa, "money")
    answers, _ = services.parse(s, suggested(s))
    change = adopt(services.propose(wa, s, answers, user=treasurer))
    assert change.status == ChangeRequest.Status.APPLIED
    policy = LateFeePolicy.objects.get(active=True)
    assert (policy.flat_amount, policy.grace_days, policy.interest_apr) == (D("15"), 10, 0)
    assert policy.interest_adopted_on == ADOPTED
    assert policy.effective_start == ADOPTED
    assert change.resolution.number in policy.interest_resolution
    assert rules.value(wa, "payments.allocation_order") == (
        "assessment",
        "late_fee",
        "interest",
        "other",
    )
    assert rules.value(wa, "delinquency.reminder_days") is None
    assert services.state(wa, s).done


def test_adopted_late_fee_policy_starts_on_go_live_while_setting_up(wa, treasurer, settings):
    settings.HOMEOA_FIXED_TODAY = ADOPTED.isoformat()
    make_live(wa, since=dt.date(2027, 1, 1))
    s = services.section(wa, "money")
    answers, _ = services.parse(s, suggested(s))
    adopt(services.propose(wa, s, answers, user=treasurer))
    assert LateFeePolicy.objects.get(active=True).effective_start == dt.date(2027, 1, 1)


def test_other_amount_is_accepted_and_bad_ones_are_refused(wa):
    s = services.section(wa, "money")
    data = {**suggested(s), "q0": services.OTHER, "q0-other": "$12.50"}
    answers, errors = services.parse(s, data)
    assert answers["late_fees.flat_amount"] == "12.50"
    answers, errors = services.parse(s, {**data, "q0-other": "twelve"})
    assert "q0" in errors
    answers, errors = services.parse(s, {**data, "q1": "99"})
    assert "q1" in errors  # not one of the choices, and no "Other" picked


def test_adopting_fines_creates_a_schedule_tied_to_the_resolution(assoc, treasurer):
    s = services.section(assoc, "enforcement")
    answers, _ = services.parse(s, suggested(s))
    change = adopt(services.propose(assoc, s, answers, user=treasurer))
    schedule = FineSchedule.objects.get()
    assert schedule.resolution == change.resolution
    assert schedule.adopted_on == ADOPTED
    (line,) = schedule.lines.all()
    assert line.category.name == targets.GENERAL_CATEGORY
    assert (line.first_amount, line.repeat_amount, line.continuing_amount) == (
        D("50"),
        D("100"),
        None,
    )
    assert rules.value(assoc, "enforcement.cure_days") == 14


def test_adopting_approvals_sets_bill_rules_and_limits(assoc, treasurer):
    s = services.section(assoc, "approvals")
    answers, _ = services.parse(s, suggested(s))
    adopt(services.propose(assoc, s, answers, user=treasurer))
    assoc.refresh_from_db()
    rules_by_name = {r.name: r for r in ApprovalRule.objects.filter(active=True)}
    assert rules_by_name["Setup: bills under $1,000.00"].required_approvals == 1
    assert rules_by_name["Setup: bills of $1,000.00 or more"].required_approvals == 2
    assert oneoff.limit(assoc) == D("1000")
    assert rules.value(assoc, "arc.decision_days") == 30


def test_adopting_records_policies_sets_fee_and_consent(assoc, treasurer):
    s = services.section(assoc, "records")
    answers, _ = services.parse(s, suggested(s))
    change = adopt(services.propose(assoc, s, answers, user=treasurer))
    assoc.refresh_from_db()
    assert targets.resale_fee(assoc) == D("100")
    assert assoc.settings[targets.CONSENT_SETTING]["resolution"] == change.resolution.number


def test_home_shows_setup_progress_until_every_section_is_adopted(assoc, treasurer):
    item = next(i for i in todos.collect(assoc, treasurer) if i.id == "setup-progress")
    assert item.title == "Finish association setup: 0 of 5 policies adopted"
    for s in services.sections(assoc):
        answers, _ = services.parse(s, suggested(s))
        adopt(services.propose(assoc, s, answers, user=treasurer))
    assert not [i for i in todos.collect(assoc, treasurer) if i.id == "setup-progress"]


def test_wizard_pages(assoc, treasurer, client):
    client.force_login(treasurer)
    page = client.get("/settings/setup/")
    assert page.status_code == 200
    assert b"Late fees and payments" in page.content
    page = client.get("/settings/setup/money/")
    assert page.status_code == 200
    assert b"Suggested" in page.content
    s = services.section(assoc, "money")
    resp = client.post("/settings/setup/money/", suggested(s))
    change = ChangeRequest.objects.get(kind=services.KIND)
    assert resp.status_code == 302
    assert resp["Location"] == reverse("governance:resolution", args=[change.resolution_id])
    assert b"Association setup" in client.get("/settings/").content
