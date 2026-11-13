"""Settings > Association setup > Features: the board turns optional modules on or off by
resolution, and a set that cannot apply is refused with every reason."""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers
from homeoa.access.testing import grant
from homeoa.changes.models import ChangeRequest
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.enforcement.models import Violation, ViolationCategory
from homeoa.governance import services as gov
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.plugins import modules, registry
from homeoa.plugins import services as plugin_services
from homeoa.plugins.models import ModuleSwitch
from homeoa.registry.models import Unit
from homeoa.rules import active_pack
from homeoa.setup import services

ADOPTED = dt.date(2026, 11, 12)
URL = "/settings/setup/features/"


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(
        name="Cedar Row", short_name="CR", timezone="America/Los_Angeles"
    )
    load_chart_of_accounts(a)
    Unit.all_associations.create(association=a, number="A", allocated_interest=Decimal("1"))
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


def fresh(a):
    return Association.objects.get(pk=a.pk)


def features(a):
    return services.section(fresh(a), services.FEATURES)


def answers_with(a, **on):
    """Every module as it is now, with the given ones changed."""
    current = modules.switches_for(fresh(a))
    return {
        q.target: on.get(
            q.target.removeprefix("module:"), current(q.target.removeprefix("module:"))
        )
        for q in features(a).questions
    }


def open_violation(a):
    cat = ViolationCategory.all_associations.create(association=a, name="Noise")
    return Violation.all_associations.create(
        association=a,
        unit=Unit.all_associations.get(association=a),
        category=cat,
        observed_on=ADOPTED,
        description="Loud music",
        status="open",
    )


def adopt(change):
    gov.adopt(change.resolution, adopted_on=ADOPTED)
    change.refresh_from_db()
    return change


def test_features_is_the_first_section_with_one_question_per_optional_module(assoc):
    s = services.sections(assoc)[0]
    assert s.key == "features"
    assert [q.target for q in s.questions] == [f"module:{m.id}" for m in registry.all_modules()]
    assert all(q.kind == "bool" and q.suggested is False for q in s.questions)


def test_a_module_the_pack_requires_is_a_features_question_set_on_by_law(
    assoc, treasurer, client, monkeypatch
):
    pack = dataclasses.replace(active_pack(assoc), required_modules={"polls": "Test Act 1"})
    monkeypatch.setattr("homeoa.rules.active_pack", lambda association: pack)
    polls = next(q for q in features(assoc).questions if q.target == "module:polls")
    assert polls.law.fixed is True and polls.law.citation == "Test Act 1"
    answers, errors = services.parse(features(assoc), {})
    assert not errors and answers["module:polls"] is True
    client.force_login(treasurer)
    page = client.get(URL).content.decode()
    assert "Test Act 1" in page
    assert "Set by law" in page
    assert "Also always on: compliance calendar" in page


def test_the_pack_required_features_show_with_their_citation_and_unverified_mark(
    wa, treasurer, client
):
    client.force_login(treasurer)
    page = client.get(URL).content.decode()
    assert "Records requests" in page
    assert "RCW 64.34.425; RCW 64.90.640" in page
    assert "Unverified" in page


def test_starting_answers_follow_the_current_switches(assoc, settings):
    settings.HOMEOA_MODULES_DEFAULT_ON = False
    modules.set_module(fresh(assoc), "polls", True, user=None)
    start = services.initial(fresh(assoc), features(assoc))
    assert start["module:polls"] is True
    assert start["module:violations"] is False


def test_design_approval_on_with_committees_off_is_refused(assoc, treasurer, settings):
    settings.HOMEOA_MODULES_DEFAULT_ON = False
    with pytest.raises(services.SetupError) as refused:
        services.propose(
            fresh(assoc), features(assoc), answers_with(assoc, arc=True), user=treasurer
        )
    assert refused.value.reasons == ["Design approval needs Committees turned on."]
    assert not ChangeRequest.objects.exists()


def test_turning_a_module_off_with_an_open_item_is_refused_and_lists_it(assoc, treasurer):
    open_violation(assoc)
    with pytest.raises(services.SetupError) as refused:
        services.propose(
            fresh(assoc), features(assoc), answers_with(assoc, violations=False), user=treasurer
        )
    (reason,) = refused.value.reasons
    assert reason.startswith("Violations cannot be turned off while it has open items:")


def test_every_reason_is_listed_at_once_on_the_page(assoc, treasurer, client, settings):
    open_violation(assoc)
    client.force_login(treasurer)
    s = features(assoc)
    data = {}
    for i, q in enumerate(s.questions):
        if q.target not in ("module:violations", "module:committees"):
            data[services.field_name(i)] = "on"
    resp = client.post(URL, data)
    assert resp.status_code == 400
    page = resp.content.decode()
    assert "Violations" in page and "open items" in page
    assert "Committees cannot be turned off while Design approval is on" in page
    assert not ChangeRequest.objects.exists()


def test_answers_that_change_nothing_can_still_be_proposed(assoc, treasurer):
    change = services.propose(fresh(assoc), features(assoc), answers_with(assoc), user=treasurer)
    assert change.status == ChangeRequest.Status.PENDING


def test_adopting_applies_every_switch_and_links_the_change_request(assoc, treasurer, settings):
    settings.HOMEOA_MODULES_DEFAULT_ON = False
    answers = answers_with(assoc, arc=True, committees=True, polls=True)
    change = adopt(services.propose(fresh(assoc), features(assoc), answers, user=treasurer))
    assert change.status == ChangeRequest.Status.APPLIED
    a = fresh(assoc)
    assert modules.is_on(a, "arc") and modules.is_on(a, "committees") and modules.is_on(a, "polls")
    assert not modules.is_on(a, "violations")
    linked = ModuleSwitch.all_associations.filter(change_request=change, enabled=True)
    assert set(linked.values_list("module_id", flat=True)) == {"arc", "committees", "polls"}


def test_switches_changed_by_adoption_record_who_adopted_it(assoc, treasurer, settings):
    from homeoa.audit.context import audit_context

    settings.HOMEOA_MODULES_DEFAULT_ON = False
    change = services.propose(
        fresh(assoc), features(assoc), answers_with(assoc, polls=True), user=treasurer
    )
    with audit_context(actor=treasurer):
        adopt(change)
    switch = ModuleSwitch.all_associations.get(association=assoc, module_id="polls")
    assert switch.enabled and switch.changed_by == treasurer


def test_adopting_fails_when_an_open_item_appeared_after_the_proposal(assoc, treasurer):
    answers = answers_with(assoc, violations=False, polls=False)
    change = services.propose(fresh(assoc), features(assoc), answers, user=treasurer)
    open_violation(assoc)
    change = adopt(change)
    assert change.status == ChangeRequest.Status.FAILED
    assert "open items" in change.error
    assert not ModuleSwitch.all_associations.exists()
    a = fresh(assoc)
    assert modules.is_on(a, "violations") and modules.is_on(a, "polls")


def test_the_design_approval_question_warns_about_ev_charger_and_heat_pump_deadlines(wa):
    arc = next(q for q in features(wa).questions if q.target == "module:arc")
    assert "EV charger and heat pump requests are still approved by law" in arc.note
    assert "60 days" in arc.note
    assert "RCW 64.90.513" in arc.note


def test_the_generic_pack_has_no_deemed_approval_warning(assoc):
    arc = next(q for q in features(assoc).questions if q.target == "module:arc")
    assert arc.note == ""


def test_the_resolution_says_on_and_off_for_features(assoc, treasurer):
    change = services.propose(
        fresh(assoc), features(assoc), answers_with(assoc, polls=False), user=treasurer
    )
    text = change.resolution.text
    assert "Polls: Off." in text
    assert "Violations: On." in text
    assert "Yes" not in text and "No." not in text


def test_finish_association_setup_counts_the_features_section(assoc, treasurer):
    item = next(i for i in todos.collect(assoc, treasurer) if i.id == "setup-progress")
    assert "0 of 5" in item.title
    adopt(services.propose(fresh(assoc), features(assoc), answers_with(assoc), user=treasurer))
    item = next(i for i in todos.collect(assoc, treasurer) if i.id == "setup-progress")
    assert "1 of 5" in item.title
