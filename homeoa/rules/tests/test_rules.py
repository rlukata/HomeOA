import datetime as dt
from decimal import Decimal
from importlib.metadata import entry_points

import pytest
from django.contrib.auth import get_user_model
from django.template import Context, Template
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest
from homeoa_rulepack_us_wa.pack import pack as wa_pack

from homeoa import providers, rules
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.models import Association
from homeoa.plugins import registry, services
from homeoa.rules.base import Basis, RuleValue
from homeoa.rules.keys import KEYS


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


def test_generic_defaults(assoc):
    assert rules.active_pack(assoc).id == "generic"
    assert rules.payment_allocation_order(assoc) == ("assessment", "late_fee", "interest", "other")
    assert rules.value(assoc, "payments.oldest_first") is True
    assert rules.value(assoc, "late_fees.amount") == Decimal("25.00")
    assert rules.value(assoc, "late_fees.grace_days") == 10
    assert rules.value(assoc, "interest.annual_rate") == Decimal("0")
    assert rules.late_fee_cap(assoc) is None
    assert rules.notice_window(assoc, "owner_meeting") == (10, 60)  # placeholders
    assert rules.fine_due_process_steps(assoc) is None
    assert rules.vote_threshold(assoc, "audit_waiver") is None
    r = rules.resolve(assoc, "late_fees.amount")
    assert r.basis is Basis.POLICY and "HomeOA default" in r.citation and not r.verified


def test_generic_pack_covers_every_key_and_claims_no_statute():
    pack = rules.generic_pack()
    assert set(pack.values) == set(KEYS)
    assert all(v.basis is not Basis.STATUTE for vs in pack.values.values() for v in vs)


def test_unknown_key(assoc):
    with pytest.raises(KeyError):
        rules.value(assoc, "nope")
    with pytest.raises(KeyError):
        rules.notice_window(assoc, "nope")


def test_override_precedence_and_audit(assoc, wa):
    assert rules.value(assoc, "votes.audit_waiver.threshold_percent") == Decimal("60")
    rules.set_override(
        assoc, "votes.audit_waiver.threshold_percent", "66.67", reason="Bylaws say 2/3"
    )
    assoc.refresh_from_db()
    r = rules.resolve(assoc, "votes.audit_waiver.threshold_percent")
    assert r.value == Decimal("66.67") and r.source == "override"
    assert r.pack_rule.citation == "RCW 64.34.372(1)"
    ev = AuditEvent.all_associations.get(action="rules.override")
    assert ev.changes == {
        "key": "votes.audit_waiver.threshold_percent",
        "old": None,
        "new": "66.67",
        "reason": "Bylaws say 2/3",
    }
    with pytest.raises(ValueError, match="reason"):
        rules.clear_override(assoc, "votes.audit_waiver.threshold_percent", reason=" ")
    rules.clear_override(assoc, "votes.audit_waiver.threshold_percent", reason="Back to statute")
    assert rules.value(assoc, "votes.audit_waiver.threshold_percent") == Decimal("60")
    ev = AuditEvent.all_associations.filter(action="rules.override").order_by("-pk").first()
    assert ev.changes["new"] is None and ev.changes["old"] == "66.67"
    assert ev.changes["reason"] == "Back to statute"


def test_override_requires_reason(assoc):
    with pytest.raises(ValueError, match="reason"):
        rules.set_override(assoc, "late_fees.amount", "30", reason="")
    assert "rules" not in assoc.settings


def test_rule_pack_is_explicit_only(assoc):
    registry.register(wa_manifest)
    try:
        services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
        assert rules.active_pack(assoc).id == "generic"  # enabled alone is not enough
        assert providers.warnings(assoc) == []
        providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
        assert rules.active_pack(assoc).id == "us_wa_condo"
        providers.set_choice(assoc, "RulePack", "")
        assert rules.active_pack(assoc).id == "generic"
    finally:
        registry.clear()


def test_override_on_generic(assoc):
    rules.set_override(assoc, "late_fees.amount", "35", reason="Board vote")
    rules.set_override(
        assoc,
        "payments.allocation_order",
        ["other", "assessment", "late_fee", "interest"],
        reason="v",
    )
    assert rules.value(assoc, "late_fees.amount") == Decimal("35.00")
    assert rules.payment_allocation_order(assoc)[0] == "other"
    assert assoc.settings["rules"]["late_fees.amount"] == "35.00"


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("late_fees.amount", 2.5),
        ("late_fees.amount", "-1"),
        ("late_fees.grace_days", "x"),
        ("interest.annual_rate", "101"),
        ("payments.oldest_first", "yes"),
        ("payments.allocation_order", ["assessment"]),
        ("fines.due_process_steps", ["torture"]),
    ],
)
def test_override_validation(assoc, key, raw):
    with pytest.raises(ValueError):
        rules.set_override(assoc, key, raw, reason="x")
    assert "rules" not in assoc.settings


def test_wa_pack_resolves_when_enabled(assoc, wa):
    assert rules.active_pack(assoc).id == "us_wa_condo"
    on = dt.date(2026, 10, 8)
    assert rules.value(assoc, "notices.owner_meeting.min_days", on) == 14
    assert rules.fine_due_process_steps(assoc)[0] == "adopted_fine_schedule"
    # No WA statute sets an order: generic policy value used, research note kept.
    r = rules.resolve(assoc, "payments.allocation_order", on)
    assert r.value[0] == "assessment" and r.source == "generic"
    assert "No WA statute" in r.note and r.verified is False
    other = Association.objects.create(name="Other", short_name="OT")
    assert rules.active_pack(other).id == "generic"


def test_wa_values_coerce():
    for key, vs in wa_pack.values.items():
        for v in vs:
            KEYS[rules.keys.canonical(key)].coerce(v.value)


def test_wa_entry_point_installed():
    names = [ep.name for ep in entry_points(group=registry.GROUP)]
    assert "rulepack_us_wa" in names


def test_all_values_mix_verified_and_unverified(assoc, wa):
    rows = rules.all_values(assoc, dt.date(2026, 10, 8))
    assert len(rows) == len(KEYS)
    assert any(r.verified for r in rows) and not all(r.verified for r in rows)


def test_badge_tag():
    t = Template("{% load rule_tags %}{% rule_badge v %}")
    out = t.render(Context({"v": RuleValue(value=1, basis=Basis.STATUTE)}))
    assert "Unverified" in out and "badge warn" in out
    out = t.render(Context({"v": RuleValue(value=1, basis=Basis.STATUTE, verified=True)}))
    assert "Verified" in out and "Unverified" not in out


def test_board_page(client, assoc, wa):
    assert client.get("/settings/rules/").status_code == 302
    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    resp = client.get("/settings/rules/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Washington condominium" in html and "(unverified)" not in html
    assert "RCW 64.34.372(1)" in html
    assert "(to verify)" not in html


def test_board_page_override_form(client, assoc):
    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    url = "/settings/rules/override/"
    resp = client.post(url, {"key": "late_fees.amount", "value": "30", "action": "set"})
    assert resp.status_code == 200  # reason missing
    assoc.refresh_from_db()
    assert "rules" not in assoc.settings
    resp = client.post(
        url, {"key": "late_fees.amount", "value": "30", "reason": "Vote 3/12", "action": "set"}
    )
    assert resp.status_code == 302
    assoc.refresh_from_db()
    assert rules.value(assoc, "late_fees.amount") == Decimal("30.00")
    resp = client.post(
        url, {"key": "late_fees.grace_days", "value": "soon", "reason": "x", "action": "set"}
    )
    assert resp.status_code == 200 and "expected a number" in resp.content.decode()
    resp = client.post(url, {"key": "late_fees.amount", "reason": "Undo", "action": "clear"})
    assert resp.status_code == 302
    assoc.refresh_from_db()
    assert rules.value(assoc, "late_fees.amount") == Decimal("25.00")
    assert AuditEvent.all_associations.filter(action="rules.override").count() == 2


def test_rules_pages_need_permissions(client, assoc):
    client.force_login(get_user_model().objects.create_user("owner@example.org"))
    assert client.get("/settings/rules/").status_code == 403
    data = {"key": "late_fees.amount", "value": "1", "reason": "x", "action": "set"}
    assert client.post("/settings/rules/override/", data).status_code == 403
    assert client.post("/settings/providers/", {"kind": "EmailChannel"}).status_code == 403
