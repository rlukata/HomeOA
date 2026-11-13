"""The WA rule pack carries statute-checked, effective-dated values."""

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest
from homeoa_rulepack_us_wa.pack import ESSB_5129
from homeoa_rulepack_us_wa.pack import pack as wa_pack

from homeoa import providers, rules
from homeoa.core.models import Association
from homeoa.plugins import registry, services
from homeoa.rules.base import Basis
from homeoa.rules.keys import KEYS

LAST_2027 = dt.date(2027, 12, 31)
FIRST_2028 = dt.date(2028, 1, 1)
# The research notes the quotes were copied from; they are not published with the code.
NOTES = Path(__file__).resolve().parents[3] / "docs" / "legal"


def _norm(text):
    return re.sub(r"\s+", " ", text)


@pytest.fixture
def assoc(db):
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def wa(assoc):
    registry.register(wa_manifest)
    services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    yield
    registry.clear()


def _all():
    return [(k, v) for k, vs in wa_pack.values.items() for v in vs]


def test_keys_known_and_no_to_verify_suffix():
    assert {rules.keys.canonical(k) for k in wa_pack.values} <= set(KEYS)
    for key, v in _all():
        assert "to verify" not in v.citation, key
    assert "unverified" not in str(wa_pack.name).lower()
    assert "Not legal advice" in str(wa_pack.description)
    assert "Oct 8, 2026" in str(wa_pack.description)


FEDERAL_SOURCES = {
    "26 U.S.C. ": "https://www.law.cornell.edu/uscode/text/26/",
    "12 CFR ": "https://www.ecfr.gov/",
}


def test_every_verified_value_has_quote_url_and_check_date():
    verified = [(k, v) for k, v in _all() if v.verified]
    assert len(verified) > 60
    for key, v in verified:
        assert v.quote and v.citation, key
        federal = next((u for c, u in FEDERAL_SOURCES.items() if v.citation.startswith(c)), None)
        assert v.source_url.startswith(federal or "https://app.leg.wa.gov/RCW/"), key
        assert v.checked_on in (dt.date(2026, 10, 8), dt.date(2026, 10, 11), ESSB_5129), key


@pytest.mark.skipif(not NOTES.is_dir(), reason="the research notes are not in this copy")
def test_quotes_are_copied_from_the_research():
    research = _norm(" ".join(p.read_text() for p in NOTES.glob("wa-research-2026-10-*.md")))
    for key, v in _all():
        if v.quote:
            assert _norm(v.quote) in research, key


def test_statute_values_cite_rcw_or_the_federal_code():
    for key, v in _all():
        if v.basis is Basis.STATUTE:
            assert v.citation.startswith(("RCW ", "26 U.S.C. ", "12 CFR ")), key
            assert v.source_url, key


@pytest.mark.parametrize(
    ("key", "before", "after"),
    [
        ("votes.audit_waiver.threshold_percent", Decimal("60"), Decimal("50")),
        ("meetings.owner.quorum_percent", Decimal("25"), Decimal("20")),
        ("resale.update_fee_cap", None, Decimal("100.00")),
        ("audit.cpa_audit_revenue_threshold", None, Decimal("100000.00")),
    ],
)
def test_resolution_across_2028(assoc, wa, key, before, after):
    assert rules.value(assoc, key, LAST_2027) == before
    assert rules.value(assoc, key, FIRST_2028) == after
    r = rules.resolve(assoc, key, LAST_2027)
    assert r.next_rule is not None and r.next_rule.effective_from == FIRST_2028


def test_values_in_force_now(assoc, wa):
    on = dt.date(2026, 10, 8)
    expect = {
        "late_fees.pre_notice.max_amount": Decimal("50.00"),
        "late_fees.pre_notice.max_percent": Decimal("5"),
        "late_fees.pre_notice.admin_fee_max": Decimal("10.00"),
        "interest.annual_rate_max": Decimal("12.00"),
        "resale.fee_cap": Decimal("275.00"),
        "resale.certificate_days": 10,
        "delinquency.first_notice.max_days_past_due": 30,
        "delinquency.standstill_days": 15,
        "delinquency.second_notice.min_days_past_due": 90,
        "delinquency.second_notice.min_days_after_first": 60,
        "delinquency.foreclosure.min_amount": Decimal("2000.00"),
        "budget.ratification.min_days": 14,
        "budget.ratification.max_days": 50,
        "budget.ratification.quorum_required": False,
        "notices.electronic.auto_revoke_after_failures": 2,
        "notices.electronic.prior_vendor_consent_reusable": False,
        "payments.free_method_required": True,
    }
    for key, value in expect.items():
        assert rules.value(assoc, key, on) == value, key
    assert rules.resolve(assoc, "interest.annual_rate_max", on).attorney_review
    assert "WSR" in rules.resolve(assoc, "interest.annual_rate_max", on).note


def test_caps_clamp_overrides(assoc, wa):
    on = dt.date(2026, 10, 8)
    rules.set_override(assoc, "late_fees.pre_notice.max_amount", "75", reason="test")
    rules.set_override(assoc, "resale.fee_cap", "300", reason="test")
    rules.set_override(assoc, "interest.annual_rate", "18", reason="test")
    assoc.refresh_from_db()
    for key, cap in [
        ("late_fees.pre_notice.max_amount", Decimal("50.00")),
        ("resale.fee_cap", Decimal("275.00")),
        ("interest.annual_rate", Decimal("12.00")),
    ]:
        r = rules.resolve(assoc, key, on)
        assert r.clamped and r.value == cap and r.cap_citation.startswith("RCW"), key


def test_fee_pass_through_needs_free_method_and_attorney():
    for v in wa_pack.schedule("payments.fee_pass_through_allowed"):
        assert v.value is True and v.attorney_review
        assert "declaration" in v.note and "free" in v.note
        for when in (v.effective_from, v.effective_to):
            if when:
                free = wa_pack.get("payments.free_method_required", when)
                assert free is not None and free.value is True
    assert wa_pack.get("payments.fee_pass_through_allowed", dt.date(2025, 12, 31)) is None


def test_payment_order_stays_policy_with_negative_finding():
    for key in ("payments.allocation_order", "payments.allocation_mode"):
        (v,) = wa_pack.schedule(key)
        assert v.basis is Basis.POLICY and v.value is None and not v.verified
        assert "No WA statute" in v.note


def test_attorney_flags():
    on = dt.date(2026, 10, 8)
    for key in (
        "reserves.study_required",
        "payments.fee_pass_through_allowed",
        "notices.electronic.prior_vendor_consent_reusable",
        "interest.annual_rate_max",
    ):
        assert wa_pack.get(key, on).attorney_review, key


def test_demo_seed_uses_wa_pack(db, settings):
    from homeoa.core import demo

    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    assert rules.active_pack(a).id == "us_wa_condo"
    r = rules.resolve(a, "notices.owner_meeting.min_days")
    assert r.value == 14 and r.verified and r.quote
    assert rules.resolve(a, "resale.update_fee_cap").next_value == Decimal("100.00")


# --- governance and meeting values ---------------------------------------------

M4_KEYS = (
    "notices.owner_meeting.min_days",
    "notices.owner_meeting.max_days",
    "notices.special_meeting.min_days",
    "notices.special_meeting.max_days",
    "notices.board_meeting.min_days",
    "notices.board_meeting.emergency_min_days",
    "meetings.owner.quorum_percent",
    "budget.distribute_within_days",
    "budget.ratification.min_days",
    "budget.ratification.max_days",
    "budget.rejection_percent_of_all_votes",
    "budget.ratification.quorum_required",
    "votes.audit_waiver.threshold_percent",
    "votes.ballot_retention_months",
    "votes.electronic.express_authority",
    "votes.paper_ballot_for_nonconsenting",
)
M4_NEW = M4_KEYS[4:6] + M4_KEYS[-3:]


@pytest.mark.parametrize("key", M4_KEYS)
def test_governance_keys_resolve_in_generic_and_wa(assoc, key):
    generic = rules.resolve(assoc, key)
    assert generic.source == "generic"
    for day in (LAST_2027, FIRST_2028):
        rule = wa_pack.get(key, day)
        assert rule is not None and rule.value is not None, (key, day)
        assert rule.citation.startswith("RCW "), key


@pytest.mark.parametrize("key", M4_NEW)
def test_governance_values_checked_against_the_statute(key):
    for day in (LAST_2027, FIRST_2028):
        rule = wa_pack.get(key, day)
        assert rule.verified and rule.quote, (key, day)


def test_two_signatures_on_reserve_spending_are_statute_before_and_after_2028():
    now, later = wa_pack.values["reserves.spend_min_approvals"]
    assert (now.value, now.basis, now.citation) == (2, Basis.STATUTE, "RCW 64.34.372(2)")
    assert (later.value, later.basis, later.verified) == (2, Basis.STATUTE, True)
    assert later.citation == "RCW 64.90.535(3)(a)" and later.effective_from == FIRST_2028


def test_records_deadline_is_policy_until_2028_then_statute():
    before = wa_pack.get("records.inspection_days", LAST_2027)
    after = wa_pack.get("records.inspection_days", FIRST_2028)
    assert (before.value, before.basis, before.verified) == (10, Basis.POLICY, False)
    assert (after.value, after.basis, after.verified) == (10, Basis.STATUTE, True)
    assert wa_pack.get("records.inspection_max_days", FIRST_2028).value == 21


def test_resale_form_switches_to_the_2028_certificate():
    from homeoa.resale import catalog

    assert wa_pack.get("resale.certificate_form", LAST_2027).value == "wa_64_34_425"
    assert wa_pack.get("resale.certificate_form", FIRST_2028).value == "wa_64_90_640"
    assert [i.key for i in catalog.get("wa_64_34_425").items] == list("abcdefghijklmnopqrst")
    assert [i.key for i in catalog.get("wa_64_90_640").items] == list("abcdefghijklmnopqrstuvwxyz")


def test_every_2028_delinquency_value_is_quoted():
    for key in wa_pack.values:
        if key.startswith(("delinquency.", "late_fees.pre_notice.")):
            rule = wa_pack.get(key, FIRST_2028)
            assert rule.verified and rule.citation.startswith("RCW 64.90.485"), key


@pytest.mark.parametrize(
    ("key", "before", "after"),
    [
        ("votes.audit_waiver.threshold_percent", Decimal("60"), Decimal("50")),
        ("votes.electronic.express_authority", False, True),
        ("meetings.owner.quorum_percent", Decimal("25"), Decimal("20")),
    ],
)
def test_governance_effective_date_switch(assoc, wa, key, before, after):
    assert rules.value(assoc, key, LAST_2027) == before
    assert rules.value(assoc, key, FIRST_2028) == after


def test_governance_override_precedence_and_audit(assoc, wa):
    from homeoa.audit.models import AuditEvent

    rules.set_override(assoc, "notices.board_meeting.min_days", 21, reason="Bylaws say 21")
    r = rules.resolve(assoc, "notices.board_meeting.min_days")
    assert (r.value, r.source) == (21, "override")
    assert AuditEvent.all_associations.filter(
        action="rules.override", message="Bylaws say 21"
    ).exists()
    rules.clear_override(assoc, "notices.board_meeting.min_days", reason="back")
    assert rules.resolve(assoc, "notices.board_meeting.min_days").source == wa_pack.id


def test_governance_admin_page_badges_and_generic_source(assoc, wa, client):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    html = client.get("/settings/rules/").content.decode()
    row = html.split('data-rule-key="votes.ballot_retention_months"')[1].split("</tr>")[0]
    assert "Verified" in row and "RCW 64.34.372(3)(n)" in row
    row = html.split('data-rule-key="enforcement.cure_days"')[1].split("</tr>")[0]
    assert "Unverified" in row
    assert 'data-source="generic"' in html  # a key the WA pack leaves to the generic pack
