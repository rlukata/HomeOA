"""Turning a module on or off is refused while something stands in the way, and a
switched-off module's records stay in the audit log."""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from homeoa.access.testing import grant
from homeoa.accounts.models import User
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.enforcement.models import Violation, ViolationCategory
from homeoa.payments.models import AutopayCharge, AutopayEnrollment, AutopayStatus
from homeoa.plugins import modules
from homeoa.plugins.models import ModuleSwitch
from homeoa.registry.models import OwnershipPeriod, Person, Unit
from homeoa.rules import active_pack

TODAY = dt.date(2026, 10, 14)


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    a = Association.objects.create(name="Birch Lane", short_name="BL", timezone="UTC")
    Unit.all_associations.create(association=a, number="A", allocated_interest=Decimal("1"))
    return a


def fresh(a):
    return Association.objects.get(pk=a.pk)


def unit(a):
    return Unit.all_associations.get(association=a, number="A")


def violation(a, status="open"):
    cat = ViolationCategory.all_associations.get_or_create(association=a, name="Noise")[0]
    return Violation.all_associations.create(
        association=a,
        unit=unit(a),
        category=cat,
        observed_on=TODAY,
        description="Loud music",
        status=status,
    )


def test_violations_cannot_be_turned_off_with_an_open_violation_until_it_is_closed(assoc):
    v = violation(assoc)
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_module(fresh(assoc), "violations", False, user=None)
    assert len(refused.value.open_items) == 1
    assert "open items" in refused.value.reason
    assert not ModuleSwitch.all_associations.filter(module_id="violations").exists()
    v.status = "closed"
    v.save()
    modules.set_module(fresh(assoc), "violations", False, user=None)
    assert not modules.is_on(fresh(assoc), "violations")


def test_online_payments_cannot_be_turned_off_inside_a_recent_debits_return_window(assoc):
    person = Person.all_associations.create(association=assoc, full_name="Owner")
    period = OwnershipPeriod.all_associations.create(
        association=assoc, unit=unit(assoc), start=dt.date(2020, 1, 1)
    )
    e = AutopayEnrollment.all_associations.create(
        association=assoc,
        unit=unit(assoc),
        ownership_period=period,
        person=person,
        rule="balance",
        mandate_ref="pm_test",
        starts_on=dt.date(2026, 1, 1),
        cancelled_at=dt.datetime(2026, 10, 13, tzinfo=dt.UTC),
    )
    charge = AutopayCharge.all_associations.create(
        association=assoc,
        enrollment=e,
        unit=unit(assoc),
        period=dt.date(2026, 10, 1),
        amount="300.00",
        status=AutopayStatus.PENDING,
        debit_on=dt.date(2026, 10, 9),
    )
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_module(fresh(assoc), "online_payments", False, user=None)
    assert len(refused.value.open_items) == 1
    charge.debit_on = dt.date(2026, 9, 23)  # outside the return window: settled
    charge.save()
    modules.set_module(fresh(assoc), "online_payments", False, user=None)
    assert not modules.is_on(fresh(assoc), "online_payments")


def test_a_module_the_rule_pack_requires_cannot_be_turned_off(assoc, monkeypatch):
    pack = dataclasses.replace(active_pack(assoc), required_modules={"calendar": "Test Act 1.2"})
    monkeypatch.setattr("homeoa.rules.active_pack", lambda association: pack)
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_module(fresh(assoc), "calendar", False, user=None)
    assert refused.value.citation == "Test Act 1.2"
    assert "Test Act 1.2" in refused.value.reason


def test_committees_cannot_be_turned_off_while_design_approval_is_on(assoc):
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_module(fresh(assoc), "committees", False, user=None)
    assert refused.value.required_by == ["arc"]
    modules.set_module(fresh(assoc), "arc", False, user=None)
    modules.set_module(fresh(assoc), "committees", False, user=None)
    assert not modules.is_on(fresh(assoc), "committees")


def test_design_approval_cannot_be_turned_on_while_committees_is_off(assoc):
    modules.set_module(fresh(assoc), "arc", False, user=None)
    modules.set_module(fresh(assoc), "committees", False, user=None)
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_module(fresh(assoc), "arc", True, user=None)
    assert refused.value.missing == ["committees"]
    modules.set_module(fresh(assoc), "committees", True, user=None)
    modules.set_module(fresh(assoc), "arc", True, user=None)


def test_a_call_that_changes_nothing_is_never_refused(assoc):
    violation(assoc)
    modules.set_module(fresh(assoc), "violations", True, user=None)
    modules.set_module(fresh(assoc), "committees", True, user=None)


def test_the_command_prints_why_it_was_refused_and_fails(assoc):
    violation(assoc)
    with pytest.raises(CommandError, match="open items"):
        call_command("module", "off", "violations", "--association", "BL")


def test_a_switched_off_modules_records_stay_in_the_audit_log(assoc, client):
    with association_context(assoc):
        violation(assoc, status="closed")
    modules.set_module(fresh(assoc), "violations", False, user=None)
    user = User.objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    resp = client.get("/settings/audit/", {"object_type": "enforcement.violation"})
    assert resp.status_code == 200
    assert [e.action for e in resp.context["page_obj"]] == ["create"]


def test_the_demo_turns_every_module_on_for_a_new_association(assoc, settings):
    from homeoa.core.demo import _modules
    from homeoa.plugins import registry

    settings.HOMEOA_MODULES_DEFAULT_ON = False
    _modules(assoc)
    assert modules.enabled_modules(fresh(assoc)) == {m.id for m in registry.all_modules()}


def test_a_refusal_lists_every_reason_at_once(assoc, monkeypatch):
    violation(assoc)
    pack = dataclasses.replace(active_pack(assoc), required_modules={"violations": "Test Act 3"})
    monkeypatch.setattr("homeoa.rules.active_pack", lambda association: pack)
    with pytest.raises(modules.ModuleChangeRefused) as refused:
        modules.set_module(fresh(assoc), "violations", False, user=None)
    assert len(refused.value.reasons) == 2
    assert refused.value.citation == "Test Act 3"
    assert len(refused.value.open_items) == 1
    with pytest.raises(CommandError) as failed:
        call_command("module", "off", "violations", "--association", "BL")
    assert "Test Act 3" in str(failed.value) and "open items" in str(failed.value)


def test_the_unit_file_keeps_a_switched_off_features_entries_without_links(assoc, rf):
    from homeoa.documents import unitfile

    violation(assoc, status="closed")
    user = User.objects.create_user("board@example.org")
    grant(user, association=assoc)
    request = rf.get("/")
    request.user, request.association = user, assoc

    def violation_entries():
        a = fresh(assoc)
        unit_a = Unit.all_associations.get(association=a, number="A")
        return [e for e in unitfile.entries(unit_a, request) if e.kind == "violations"]

    assert violation_entries()[0].url
    modules.set_module(fresh(assoc), "violations", False, user=None)
    (entry,) = violation_entries()
    assert entry.title == "Noise" and entry.url == ""
