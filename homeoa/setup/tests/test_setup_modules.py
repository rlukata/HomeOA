"""Association setup wizard: sections and questions about an optional module show only
while that module is on, and the module applies its own answers."""

import pytest
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.plugins import registry, slots
from homeoa.plugins import services as plugin_services
from homeoa.rules.packs.generic import pack as GENERIC
from homeoa.setup import services


@pytest.fixture
def assoc(db):
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    with association_context(a):
        yield a


def _modules(setup):
    tags = {s.key: s.module for s in setup}
    tags.update({q.target: q.module for s in setup for q in s.questions if q.module})
    return tags


def test_setup_items_about_a_module_name_it_in_every_pack(assoc):
    registry.register(wa_manifest)
    plugin_services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    expected = {
        "money": None,
        "enforcement": "violations",
        "approvals": None,
        "records": None,
        "maintenance.oneoff_limit": "work_orders",
        "rule:arc.decision_days": "arc",
    }
    assert _modules(GENERIC.setup) == expected
    assert _modules(services.sections(assoc)) == {"features": None, **expected}


def test_sections_and_questions_of_a_module_that_is_off_are_hidden(assoc, monkeypatch):
    monkeypatch.setattr(
        slots, "module_on", lambda m, association=None: m not in {"violations", "arc"}
    )
    sections = {s.key: s for s in services.sections(assoc)}
    assert list(sections) == ["features", "money", "approvals", "records"]
    targets = [q.target for q in sections["approvals"].questions]
    assert targets == ["bills.two_approvals_from", "maintenance.oneoff_limit"]
    assert services.section(assoc, "enforcement") is None


def test_setup_progress_counts_only_the_sections_shown(assoc, monkeypatch):
    (item,) = services.todo_items(assoc)
    assert "0 of 5" in str(item["title"])
    monkeypatch.setattr(slots, "module_on", lambda m, association=None: m != "violations")
    (item,) = services.todo_items(assoc)
    assert "0 of 4" in str(item["title"])


def test_modules_register_appliers_for_their_answers():
    appliers = dict(slots.values(slots.WIZARD_SECTIONS))
    assert appliers["fines."].__module__ == "homeoa.enforcement.setup_answers"
    assert appliers["maintenance.oneoff_limit"].__module__ == "homeoa.maintenance.oneoff"
