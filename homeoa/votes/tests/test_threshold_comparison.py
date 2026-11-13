"""The threshold comparison ("gte" / "gt") comes from the rule pack."""

import datetime as dt
from decimal import Decimal

import pytest
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest
from homeoa_rulepack_us_wa.pack import pack as wa_pack

from homeoa import providers, rules
from homeoa.plugins import registry as plugin_registry
from homeoa.plugins import services as plugin_services
from homeoa.rules.keys import KEYS, comparison_key
from homeoa.votes import services
from homeoa.votes.models import Threshold

from .test_votes import assoc, cast, make_vote, units  # noqa: F401

UTC = dt.UTC
LAST_2027 = dt.date(2027, 12, 31)
FIRST_2028 = dt.date(2028, 1, 1)


@pytest.fixture
def wa(assoc):  # noqa: F811
    plugin_registry.register(wa_manifest)
    plugin_services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    yield
    plugin_registry.clear()


def _all_yes(vote, units):  # noqa: F811
    for n in units:
        cast(vote, units, n, "Yes")


@pytest.mark.parametrize(("cmp", "passed"), [("gte", True), ("gt", False)])
def test_exactly_at_threshold(assoc, units, cmp, passed):  # noqa: F811
    rules.set_override(assoc, "votes.audit_waiver.threshold_percent", "100", reason="t")
    rules.set_override(assoc, "votes.audit_waiver.threshold_comparison", cmp, reason="t")
    v = make_vote(assoc, threshold=Threshold.RULE_PACK, pack_kind="audit_waiver")
    _all_yes(v, units)  # 9/9 = exactly 100%
    assert services.tally(v).questions[0].passed is passed


def test_generic_default_is_more_than(assoc, units):  # noqa: F811
    for key in ("votes.audit_waiver.threshold_comparison", "budget.rejection_comparison"):
        r = rules.resolve(assoc, key)
        assert (r.value, r.source, r.verified) == ("gt", "generic", False)
    rules.set_override(assoc, "budget.rejection_percent_of_all_votes", "100", reason="t")
    v = make_vote(assoc, threshold=Threshold.RULE_PACK, pack_kind="budget_rejection")
    _all_yes(v, units)
    rule = services.threshold_rule(v)
    assert rule.comparison == "more_than"
    assert services.tally(v).questions[0].passed is False


def test_explicit_vote_comparison_wins(assoc, units):  # noqa: F811
    v = make_vote(
        assoc, threshold=Threshold.RULE_PACK, pack_kind="audit_waiver", comparison="at_least"
    )
    rules.set_override(assoc, "votes.audit_waiver.threshold_percent", "60", reason="t")
    assert services.threshold_rule(v).comparison == "at_least"


@pytest.mark.parametrize(
    ("closes", "percent", "comparison"),
    [(LAST_2027, Decimal("60"), "at_least"), (FIRST_2028, Decimal("50"), "more_than")],
)
def test_wa_2027_2028_switch(assoc, units, wa, closes, percent, comparison):  # noqa: F811
    closes_at = dt.datetime.combine(closes, dt.time(18), tzinfo=UTC)
    v = make_vote(
        assoc,
        threshold=Threshold.RULE_PACK,
        pack_kind="audit_waiver",
        opens_at=closes_at - dt.timedelta(days=10),
        closes_at=closes_at,
    )
    rule = services.threshold_rule(v)
    assert (rule.percent, rule.comparison) == (percent, comparison)


def test_wa_budget_rejection_more_than(assoc, units, wa):  # noqa: F811
    v = make_vote(assoc, threshold=Threshold.RULE_PACK, pack_kind="budget_rejection")
    assert services.threshold_rule(v).comparison == "more_than"


@pytest.mark.parametrize(
    "key", ["votes.audit_waiver.threshold_comparison", "budget.rejection_comparison"]
)
def test_wa_comparisons_quoted_and_cited(key):
    for day in (LAST_2027, FIRST_2028):
        rule = wa_pack.get(key, day)
        assert rule.value in ("gte", "gt") and rule.verified and rule.quote
        assert rule.citation.startswith("RCW ")


def test_wa_comparison_values():
    key = "votes.audit_waiver.threshold_comparison"
    assert wa_pack.get(key, LAST_2027).value == "gte"
    assert wa_pack.get(key, FIRST_2028).value == "gt"
    assert wa_pack.get("budget.rejection_comparison", FIRST_2028).value == "gt"


def test_verified_badge_on_rules_page(assoc, wa, client):  # noqa: F811
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    html = client.get("/settings/rules/").content.decode()
    row = html.split('data-rule-key="budget.rejection_comparison"')[1].split("</tr>")[0]
    assert "Verified" in row and "Unverified" not in row and "RCW 64.90.525(1)" in row


def test_every_threshold_key_has_a_comparison_key():
    for key in KEYS:
        if key.endswith("threshold_percent") or key == "budget.rejection_percent_of_all_votes":
            assert comparison_key(key) in KEYS
    assert comparison_key("votes.budget_rejection.threshold_percent") == (
        "budget.rejection_comparison"
    )


def test_bad_override_rejected(assoc):  # noqa: F811
    with pytest.raises(ValueError, match="must be one of"):
        rules.set_override(assoc, "budget.rejection_comparison", "ge", reason="t")


def test_months_kind_message(assoc):  # noqa: F811
    with pytest.raises(ValueError, match="expected a number of months"):
        rules.set_override(assoc, "votes.ballot_retention_months", "a year", reason="t")


def test_days_kind_message_unchanged():
    with pytest.raises(ValueError, match="expected a number of days"):
        KEYS["notices.owner_meeting.min_days"].coerce("x")
