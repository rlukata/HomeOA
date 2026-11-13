import pytest
from django.conf import settings

from homeoa.plugins import registry, slots
from homeoa.plugins.tests.fixtures import modules as fx

FIXTURES = "homeoa.plugins.tests.fixtures.modules"

EXPECTED = {
    "violations": (),
    "arc": ("committees",),
    "owner_requests": (),
    "work_orders": (),
    "announcements": (),
    "calendar": (),
    "polls": (),
    "directory": (),
    "committees": (),
    "online_payments": (),
    "board_packet": (),
    "ai_assistance": (),
}


@pytest.fixture
def restore_modules():
    yield
    registry.load_modules(settings.HOMEOA_MODULES)


@pytest.fixture
def clean_slots():
    saved = {k: list(v) for k, v in slots._contributions.items()}
    slots.clear()
    yield
    slots.clear()
    slots._contributions.update(saved)


def test_every_optional_feature_is_registered_as_a_module_with_its_requirements():
    found = {m.id: m for m in registry.all_modules()}
    assert set(found) == set(EXPECTED)
    for module_id, requires in EXPECTED.items():
        assert found[module_id].kind == "module"
        assert found[module_id].requires == requires
        assert str(found[module_id].name)


def test_owner_votes_are_not_a_module():
    assert registry.module("votes") is None


def test_a_module_requiring_an_unknown_module_fails_to_load(restore_modules):
    with pytest.raises(registry.PluginError, match="nope"):
        registry.load_modules([f"{FIXTURES}.needs_missing"])


def test_a_plugin_manifest_cannot_be_listed_as_a_module(restore_modules):
    with pytest.raises(registry.PluginError, match="not a module manifest"):
        registry.load_modules(["homeoa.plugins.tests.fixtures.homeoa_testplugin.manifest"])


def test_modules_are_not_listed_as_plugins():
    plugin_ids = {m.id for m in registry.all_plugins()}
    assert not plugin_ids & set(EXPECTED)


@pytest.mark.django_db
def test_modules_are_not_shown_on_the_plugins_settings_page(client):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant
    from homeoa.core.models import Association

    assoc = Association.objects.create(name="Maple Court", short_name="MC")
    user = get_user_model().objects.create_user("admin@example.org")
    grant(user, "sys_admin", association=assoc)
    client.force_login(user)
    content = client.get("/settings/plugins/").content.decode()
    assert "Email bounces" in content
    assert "Resident directory" not in content
    assert "Board packet" not in content


def test_a_module_ready_hook_adds_its_contributions_tagged_with_its_id(
    restore_modules, clean_slots
):
    registry.load_modules([f"{FIXTURES}.sample"])
    registry.run_module_ready_hooks()
    [contribution] = slots.contributions(slots.HOME_CARDS)
    assert contribution.value == "sample card"
    assert contribution.module == "sample"
    assert fx.sample.id == "sample"


def test_core_and_module_contributions_share_a_slot_in_order(clean_slots):
    slots.register(slots.MENU_TABS, "core tab")
    slots.register(slots.MENU_TABS, "module tab", module="sample")
    slots.register(slots.MENU_TABS, "first tab", order=-1)
    found = slots.contributions(slots.MENU_TABS, association=None)
    assert [(c.value, c.module) for c in found] == [
        ("first tab", None),
        ("core tab", None),
        ("module tab", "sample"),
    ]
    assert slots.values(slots.MENU_TABS) == ["first tab", "core tab", "module tab"]


def test_unknown_slot_names_are_refused(clean_slots):
    with pytest.raises(ValueError):
        slots.register("no_such_slot", "x")
