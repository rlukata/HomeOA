"""Switching several modules at once is judged against the set they leave, and applies
all or nothing."""

import pytest

from homeoa.core.models import Association
from homeoa.plugins import modules
from homeoa.plugins.models import ModuleSwitch


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_MODULES_DEFAULT_ON = False
    return Association.objects.create(name="Birch Lane", short_name="BL", timezone="UTC")


def fresh(a):
    return Association.objects.get(pk=a.pk)


def test_turning_on_a_module_with_its_requirement_in_the_same_set_works(assoc):
    modules.set_modules(fresh(assoc), {"arc": True, "committees": True}, user=None)
    a = fresh(assoc)
    assert modules.is_on(a, "arc") and modules.is_on(a, "committees")


def test_turning_off_a_module_with_the_one_that_needs_it_in_the_same_set_works(assoc):
    modules.set_modules(fresh(assoc), {"arc": True, "committees": True}, user=None)
    modules.set_modules(fresh(assoc), {"arc": False, "committees": False}, user=None)
    a = fresh(assoc)
    assert not modules.is_on(a, "arc") and not modules.is_on(a, "committees")


def test_a_set_that_cannot_apply_lists_every_reason_and_changes_nothing(assoc):
    modules.set_modules(fresh(assoc), {"arc": True, "committees": True}, user=None)
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_modules(
            fresh(assoc), {"committees": False, "polls": True, "work_orders": True}, user=None
        )
    assert refused.value.reasons == [
        "Committees cannot be turned off while Design approval is on, which needs it."
    ]
    assert not ModuleSwitch.all_associations.filter(module_id="polls").exists()


def test_a_pack_required_module_cannot_be_switched_off_in_a_set(assoc, monkeypatch):
    monkeypatch.setattr(
        modules, "required_by_pack", lambda a, m: "Test Act 1" if m == "polls" else None
    )
    refused = modules.set_refusal(fresh(assoc), {"polls": False, "arc": True})
    assert refused.reasons == [
        "Design approval needs Committees turned on.",
        "Polls is required by law (Test Act 1) and cannot be turned off.",
    ]
