"""Per-association module switches: reading, setting, the starting migration, the
rule pack's required modules and the development command."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings
from homeoa_rulepack_us_wa.manifest import manifest as wa_manifest

from homeoa import providers
from homeoa.accounts.models import User
from homeoa.audit.models import AuditEvent
from homeoa.core.models import Association
from homeoa.plugins import modules, registry, services
from homeoa.plugins.models import ModuleSwitch

MODULE_IDS = {
    "violations",
    "arc",
    "owner_requests",
    "work_orders",
    "announcements",
    "calendar",
    "polls",
    "directory",
    "committees",
    "online_payments",
    "board_packet",
    "ai_assistance",
}


@pytest.fixture
def assoc(db):
    return Association.objects.create(name="Birch Lane", short_name="BL")


def fresh(assoc):
    return Association.objects.get(pk=assoc.pk)


@override_settings(HOMEOA_MODULES_DEFAULT_ON=False)
def test_a_module_with_no_switch_is_off(assoc):
    assert modules.is_on(assoc, "polls") is False
    assert modules.enabled_modules(assoc) == set()


def test_test_settings_turn_modules_on_when_there_is_no_switch(assoc):
    assert modules.enabled_modules(assoc) == MODULE_IDS


def test_a_switch_row_wins_over_the_default(assoc):
    with override_settings(HOMEOA_MODULES_DEFAULT_ON=False):
        modules.set_module(assoc, "polls", True, user=None)
        assert modules.is_on(fresh(assoc), "polls") is True
    modules.set_module(assoc, "calendar", False, user=None)
    assert modules.is_on(fresh(assoc), "calendar") is False


def test_setting_a_module_writes_the_switch_and_an_audit_event(assoc):
    user = User.objects.create_user(email="board@example.com")
    modules.set_module(assoc, "polls", True, user=user)
    modules.set_module(assoc, "polls", False, user=user)
    switch = ModuleSwitch.all_associations.get(association=assoc, module_id="polls")
    assert switch.enabled is False and switch.changed_by == user and switch.changed_at
    events = AuditEvent.all_associations.filter(
        association=assoc, object_type="homeoa_plugins.moduleswitch"
    ).order_by("pk")
    assert [e.action for e in events] == ["create", "update"]
    assert events[1].actor_id == user.pk
    assert events[1].changes["enabled"] == [True, False]
    assert modules.is_on(assoc, "polls") is False


def test_an_unknown_module_is_refused(assoc):
    with pytest.raises(modules.UnknownModule):
        modules.set_module(assoc, "laundry", True, user=None)
    with pytest.raises(modules.UnknownModule):
        modules.is_on(assoc, "laundry")
    assert not ModuleSwitch.all_associations.exists()


def test_no_module_is_required_by_the_generic_pack(assoc):
    assert all(modules.required_by_pack(assoc, m) is None for m in MODULE_IDS)


def test_no_module_is_required_by_the_washington_pack_and_core_features_are_cited(assoc):
    registry.register(wa_manifest)
    services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    assert all(modules.required_by_pack(assoc, m) is None for m in MODULE_IDS)
    from homeoa.rules import active_pack

    features = {f.id: f for f in active_pack(assoc).required_features}
    assert set(features) == {
        "records_requests",
        "resale_certificates",
        "budget_ratification",
        "meetings_and_minutes",
    }
    assert all(f.citation and not f.verified for f in features.values())


def test_the_command_switches_a_module_and_lists_them(assoc, capsys):
    with override_settings(HOMEOA_MODULES_DEFAULT_ON=False):
        call_command("module", "on", "polls", "--association", "BL")
        assert modules.is_on(fresh(assoc), "polls") is True
        call_command("module", "list")
        out = capsys.readouterr().out
        assert f"{assoc.pk}\tpolls\ton" in out
        assert f"{assoc.pk}\tcalendar\toff" in out
        call_command("module", "off", "polls", "--all")
        assert modules.is_on(fresh(assoc), "polls") is False
    event = AuditEvent.all_associations.filter(association=assoc).latest("pk")
    assert event.actor_label == "command:module"


def test_the_command_refuses_unknown_modules_and_production_settings(assoc, monkeypatch):
    with pytest.raises(CommandError):
        call_command("module", "on", "laundry")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "homeoa.settings.prod")
    with pytest.raises(CommandError):
        call_command("module", "list")


@pytest.mark.django_db(transaction=True)
def test_the_migration_turns_every_module_on_for_existing_associations():
    before = [("homeoa_plugins", "0002_module_switch")]
    after = [("homeoa_plugins", "0003_turn_on_modules_for_existing_associations")]
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(before)
        old = MigrationExecutor(connection).loader.project_state(before).apps
        assoc = old.get_model("core", "Association").objects.create(name="Old Oak", short_name="OO")
        MigrationExecutor(connection).migrate(after)
        rows = ModuleSwitch.all_associations.filter(association_id=assoc.pk)
        # AI assistance came later and stays off until the board turns it on.
        assert {r.module_id for r in rows} == MODULE_IDS - {"ai_assistance"}
        assert all(r.enabled for r in rows)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
