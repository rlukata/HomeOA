"""Effective-dated values, citations, caps clamping overrides, renamed keys."""

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa import providers, rules
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.models import Association
from homeoa.plugins import registry, services
from homeoa.plugins.manifest import PluginManifest
from homeoa.rules.base import Basis, RulePack, RulePackError, RuleValue
from homeoa.rules.keys import KEYS, RENAMED

D = datetime.date
CHECKED = D(2026, 10, 8)
URL = "https://app.leg.wa.gov/RCW/default.aspx?cite=64.90.640"


def _stat(value, **kw):
    kw.setdefault("citation", "RCW 64.90.640")
    kw.setdefault("quote", "may not exceed")
    kw.setdefault("checked_on", CHECKED)
    kw.setdefault("source_url", URL)
    return RuleValue(value=value, basis=Basis.STATUTE, verified=True, **kw)


TEST_PACK = RulePack(
    id="test_pack",
    name="Test pack",
    jurisdiction="US-XX",
    values={
        "resale.update_fee_cap": [
            _stat(None, effective_to=D(2027, 12, 31)),
            _stat(Decimal("100.00"), effective_from=D(2028, 1, 1)),
        ],
        "meetings.owner.quorum_percent": [
            _stat(Decimal("25"), effective_to=D(2027, 12, 31), citation="RCW 64.34.336"),
            _stat(Decimal("20"), effective_from=D(2028, 1, 1), attorney_review=True),
        ],
        "interest.annual_rate_max": _stat(Decimal("12"), citation="RCW 19.52.020"),
        "resale.fee_cap": _stat(Decimal("275.00")),
        # an old key name: still loads, under the new name
        "votes.budget_rejection.threshold_percent": _stat(Decimal("50"), citation="RCW 64.90"),
    },
)


@pytest.fixture
def assoc(db):
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def pack(assoc):
    registry.register(
        PluginManifest(
            id="test_pack",
            name="Test",
            version="1",
            providers={"RulePack": "homeoa.rules.tests.test_rules_dates_caps.TEST_PACK"},
        )
    )
    services.save_installation(assoc, "test_pack", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "test_pack")
    yield
    registry.clear()


def test_resolution_by_date(assoc, pack, settings):
    settings.HOMEOA_FIXED_TODAY = "2027-12-31"
    r = rules.resolve(assoc, "meetings.owner.quorum_percent")
    assert r.value == Decimal("25") and r.citation == "RCW 64.34.336" and r.verified
    assert r.next_value == Decimal("20") and r.next_rule.effective_from == D(2028, 1, 1)
    assert rules.value(assoc, "resale.update_fee_cap") is None
    settings.HOMEOA_FIXED_TODAY = "2028-01-01"
    r = rules.resolve(assoc, "meetings.owner.quorum_percent")
    assert r.value == Decimal("20") and r.attorney_review and r.next_rule is None
    assert rules.value(assoc, "resale.update_fee_cap") == Decimal("100.00")
    # an explicit as_of wins over today
    assert rules.value(assoc, "meetings.owner.quorum_percent", D(2026, 1, 1)) == Decimal("25")


def _one(values):
    return RulePack(id="x", name="x", jurisdiction="", values=values)


def test_overlap_refused():
    with pytest.raises(RulePackError, match="overlap"):
        _one(
            {
                "resale.fee_cap": [
                    RuleValue(value="1", basis=Basis.STATUTE, effective_to=D(2028, 1, 1)),
                    RuleValue(value="2", basis=Basis.STATUTE, effective_from=D(2028, 1, 1)),
                ]
            }
        )
    with pytest.raises(RulePackError, match="overlap"):  # open-ended plus dated
        _one(
            {
                "resale.fee_cap": [
                    RuleValue(value="1", basis=Basis.STATUTE),
                    RuleValue(value="2", basis=Basis.STATUTE, effective_from=D(2028, 1, 1)),
                ]
            }
        )
    with pytest.raises(RulePackError, match="before it starts"):
        _one(
            {
                "resale.fee_cap": RuleValue(
                    value="1",
                    basis=Basis.STATUTE,
                    effective_from=D(2028, 1, 1),
                    effective_to=D(2027, 1, 1),
                )
            }
        )


@pytest.mark.parametrize("blank", [{"citation": ""}, {"quote": ""}, {"checked_on": None}], ids=str)
def test_verified_needs_citation_quote_and_check_date(blank):
    kw = {"citation": "RCW 1", "quote": "q", "checked_on": CHECKED} | blank
    with pytest.raises(RulePackError, match="verified"):
        _one({"resale.fee_cap": RuleValue(value="1", basis=Basis.STATUTE, verified=True, **kw)})


def test_override_clamped_and_audited(assoc, pack, settings):
    settings.HOMEOA_FIXED_TODAY = "2026-10-08"
    rules.set_override(assoc, "interest.annual_rate", "18", reason="Declaration says 18%")
    assoc.refresh_from_db()
    r = rules.resolve(assoc, "interest.annual_rate")
    assert r.value == Decimal("12") and r.clamped and r.requested == Decimal("18")
    assert r.cap_citation == "RCW 19.52.020" and r.source == "override"
    ev = AuditEvent.all_associations.get(action="rules.override")
    assert ev.changes["new"] == "18" and ev.changes["clamped_to"] == "12"
    # self-capped keys: the board may lower them, not raise them
    rules.set_override(assoc, "resale.fee_cap", "300", reason="x")
    rules.set_override(assoc, "interest.annual_rate_max", "10", reason="Lower is fine")
    assoc.refresh_from_db()
    assert rules.value(assoc, "resale.fee_cap") == Decimal("275.00")
    r = rules.resolve(assoc, "interest.annual_rate_max")
    assert r.value == Decimal("10") and not r.clamped
    # the cap in force by date: none before 2028, $100 from 2028
    rules.set_override(assoc, "resale.update_fee_cap", "150", reason="x")
    assoc.refresh_from_db()
    assert rules.value(assoc, "resale.update_fee_cap") == Decimal("150.00")
    assert rules.value(assoc, "resale.update_fee_cap", D(2028, 1, 1)) == Decimal("100.00")


def test_pack_values_not_clamped():
    p = _one(
        {
            "interest.annual_rate": RuleValue(value="15", basis=Basis.STATUTE),
            "interest.annual_rate_max": RuleValue(value="12", basis=Basis.STATUTE),
        }
    )
    r = rules._resolve("interest.annual_rate", p, {}, CHECKED)
    assert r.value == Decimal("15") and not r.clamped


NEW_KEYS = (
    "delinquency.first_notice.max_days_past_due",
    "delinquency.standstill_days",
    "delinquency.second_notice.min_days_past_due",
    "delinquency.second_notice.min_days_after_first",
    "delinquency.foreclosure.min_months_assessments",
    "delinquency.foreclosure.min_amount",
    "delinquency.foreclosure.min_days_after_threshold",
    "delinquency.notice.mail_required",
    "delinquency.notice.email_copy_if_known",
    "late_fees.pre_notice.max_amount",
    "late_fees.pre_notice.max_percent",
    "late_fees.pre_notice.admin_fee_max",
    "interest.annual_rate_max",
    "payments.free_method_required",
    "notices.electronic.auto_revoke_after_failures",
    "notices.electronic.prior_vendor_consent_reusable",
    "budget.distribute_within_days",
    "budget.ratification.min_days",
    "budget.ratification.max_days",
    "budget.rejection_percent_of_all_votes",
    "budget.ratification.quorum_required",
    "fines.requires_prior_adopted_schedule",
    "fines.requires_notice",
    "fines.requires_hearing",
    "resale.certificate_days",
    "resale.fee_cap",
    "resale.update_fee_cap",
    "resale.update_window_months",
    "meetings.owner.quorum_percent",
)


@pytest.mark.parametrize("key", NEW_KEYS)
def test_new_keys_in_catalog_unset_in_generic(assoc, key):
    assert key in KEYS
    assert rules.value(assoc, key) is None


def test_renamed_keys_still_resolve(assoc, pack):
    assert not set(RENAMED) & set(KEYS)
    assert rules.vote_threshold(assoc, "budget_rejection") == Decimal("50")
    assert rules.notice_window(assoc, "budget_ratification") == (None, None)
    # an override saved under an old name is read, and moved on the next write
    assoc.settings = {"rules": {"notices.budget_ratification.min_days": 14}}
    assoc.save()
    assert rules.value(assoc, "budget.ratification.min_days") == 14
    rules.set_override(assoc, "notices.budget_ratification.max_days", 50, reason="x")
    rules.set_override(assoc, "budget.ratification.min_days", 15, reason="x")
    assoc.refresh_from_db()
    assert assoc.settings["rules"] == {
        "budget.ratification.min_days": 15,
        "budget.ratification.max_days": 50,
    }
    assert rules.notice_window(assoc, "budget_ratification") == (15, 50)


def test_page_shows_both_values_and_citation(client, assoc, pack, settings):
    settings.HOMEOA_FIXED_TODAY = "2026-10-08"
    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    html = client.get("/settings/rules/").content.decode()
    assert "From Jan 1, 2028: $100.00" in html
    assert "From Jan 1, 2028: 20%" in html
    assert f'href="{URL}"' in html
    assert "<details>" in html and "may not exceed" in html
    assert "Checked Oct 8, 2026" in html
    assert "Confirm with attorney" not in html  # only the 2028 value is flagged
    resp = client.post(
        "/settings/rules/override/",
        {"key": "interest.annual_rate", "value": "18", "reason": "Decl.", "action": "set"},
        follow=True,
    )
    html = resp.content.decode()
    assert "above the legal maximum" in html and "Capped by statute" in html
    assert AuditEvent.all_associations.filter(action="rules.override").count() == 1
    settings.HOMEOA_FIXED_TODAY = "2028-02-01"
    html = client.get("/settings/rules/").content.decode()
    assert "Confirm with attorney" in html
