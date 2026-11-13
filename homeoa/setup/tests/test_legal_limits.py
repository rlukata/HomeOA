"""Setup answers the law sets or limits: shown set by law, held within the limit on
submit, and worded with the statute in the resolution."""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers, rules
from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.plugins import registry
from homeoa.plugins import services as plugin_services
from homeoa.rules.base import Basis, LegalLimit, RuleValue, SetupChoice, SetupQuestion
from homeoa.setup import services

D = Decimal


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


def records(association):
    return services.section(association, "records")


def field(s, target):
    i = next(i for i, q in enumerate(s.questions) if q.target == target)
    return services.field_name(i)


def with_questions(monkeypatch, association, questions, values=None):
    """Make the active pack ask only ``questions`` in its records section."""
    pack = rules.active_pack(association)
    setup = tuple(
        dataclasses.replace(s, questions=tuple(questions)) if s.key == "records" else s
        for s in pack.setup
    )
    pack = dataclasses.replace(pack, setup=setup, values={**pack.values, **(values or {})})
    monkeypatch.setattr("homeoa.rules.active_pack", lambda association: pack)


FIXED = SetupQuestion(
    target="consent.adopt",
    label="Electronic notice consent form",
    kind="choice",
    choices=(SetupChoice(value=True, label="Adopt"), SetupChoice(value=False, label="Not now")),
    suggested=True,
    law=LegalLimit(fixed=False, citation="Test Act 7"),
)

ALLOWED = SetupQuestion(
    target="payments.allocation",
    label="What a payment pays first",
    kind="choice",
    choices=(
        SetupChoice(value="assessments_first", label="Assessments first"),
        SetupChoice(value="fees_first", label="Fees first"),
    ),
    suggested="assessments_first",
    law=LegalLimit(allowed=("assessments_first",), citation="Test Act 3"),
)


def test_a_question_set_by_law_is_not_asked_and_its_value_is_used(
    assoc, treasurer, client, monkeypatch
):
    with_questions(monkeypatch, assoc, [FIXED])
    s = records(assoc)
    answers, errors = services.parse(s, {field(s, "consent.adopt"): "True"})
    assert not errors and answers["consent.adopt"] is False
    assert "Not now (required by Test Act 7)." in services.resolution_text(assoc, s, answers)
    client.force_login(treasurer)
    page = client.get(reverse("setup:section", args=["records"])).content.decode()
    assert 'data-testid="setup-fixed"' in page and "Set by law" in page
    assert 'type="radio"' not in page


def test_a_choice_the_law_rules_out_shows_disabled_and_is_refused(
    assoc, treasurer, client, monkeypatch
):
    with_questions(monkeypatch, assoc, [ALLOWED])
    s = records(assoc)
    _answers, errors = services.parse(s, {field(s, "payments.allocation"): "fees_first"})
    assert errors[field(s, "payments.allocation")] == "Not allowed by Test Act 3."
    client.force_login(treasurer)
    page = client.get(reverse("setup:section", args=["records"])).content.decode()
    assert 'value="fees_first" disabled' in page
    assert "Not allowed by Test Act 3" in page


def test_a_resale_fee_above_the_washington_cap_is_refused_naming_the_statute(wa):
    s = records(wa)
    name = field(s, "resale.fee")
    _answers, errors = services.parse(s, {name: "275.01"})
    assert errors[name] == "At most $275.00 (RCW 64.34.425(2))."
    answers, errors = services.parse(s, {name: "275"})
    assert name not in errors and answers["resale.fee"] == "275.00"


def test_the_resolution_says_a_limited_answer_is_within_the_legal_limit(wa):
    s = records(wa)
    answers, _errors = services.parse(s, {field(s, "resale.fee"): "150"})
    text = services.resolution_text(wa, s, answers)
    assert "$150.00 (within the legal limit, RCW 64.34.425(2))." in text


def test_an_interest_rate_above_the_legal_maximum_is_refused(wa):
    s = services.section(wa, "money")
    name = field(s, "late_fees.interest_apr")
    _answers, errors = services.parse(s, {name: "12.5"})
    assert "RCW 19.52.020(1)" in errors[name]


def test_a_board_override_of_the_cap_rule_does_not_raise_the_limit(wa):
    rules.set_override(wa, "resale.fee_cap", "500", reason="test")
    s = records(wa)
    name = field(s, "resale.fee")
    _answers, errors = services.parse(s, {name: "400"})
    assert "RCW 64.34.425(2)" in errors[name]


def test_a_limit_read_from_a_rule_binds_only_from_the_rules_start_date(
    assoc, settings, monkeypatch
):
    later = RuleValue(
        value="50.00",
        basis=Basis.STATUTE,
        citation="Test Act 9",
        effective_from=dt.date(2028, 1, 1),
    )
    fee = SetupQuestion(
        target="resale.fee",
        label="Resale fee",
        kind="money",
        suggested=D("100"),
        law=LegalLimit(maximum="rule:resale.fee_cap"),
    )
    with_questions(monkeypatch, assoc, [fee], {"resale.fee_cap": (later,)})
    settings.HOMEOA_FIXED_TODAY = "2026-10-16"
    s = records(assoc)
    assert s.questions[0].law is None
    answers, errors = services.parse(s, {field(s, "resale.fee"): "100"})
    assert not errors
    settings.HOMEOA_FIXED_TODAY = "2028-01-02"
    s = records(assoc)
    _answers, errors = services.parse(s, {field(s, "resale.fee"): "100"})
    assert errors[field(s, "resale.fee")] == "At most $50.00 (Test Act 9)."


def test_a_limited_number_shows_a_slider_and_number_box_with_bounds_and_suggestion(
    wa, treasurer, client
):
    client.force_login(treasurer)
    page = client.get(reverse("setup:section", args=["records"])).content.decode()
    name = field(records(wa), "resale.fee")
    assert f'type="range" id="{name}-slider" data-mirror="{name}" min="0" max="275.00"' in page
    assert f'type="number" id="{name}" name="{name}"' in page
    assert "at most $275.00" in page
    assert "Suggested: $100.00" in page


def test_a_limit_read_from_verified_rules_is_verified_and_keeps_their_attorney_flag(wa):
    sections = {s.key: s for s in services.sections(wa)}
    fee = next(q for q in sections["records"].questions if q.target == "resale.fee")
    interest = next(q for q in sections["money"].questions if q.target == "late_fees.interest_apr")
    # The WA cap rules are checked against the statute text; the interest cap also
    # carries an attorney question.
    assert fee.law.verified and not fee.law.attorney_review
    assert interest.law.verified and interest.law.attorney_review
