"""Interest rate adoption: where the law charges its maximum rate unless the association
sets one (WA: RCW 64.34.364(13)), Home asks the board to record the rate it adopted."""

import datetime as dt

import pytest

from homeoa.billing.models import LateFeePolicy
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos
from homeoa.plugins import registry as plugin_registry


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        yield a


def _ids(a):
    return [t.id for t in todos.collect(a)]


def test_board_asked_until_a_rate_is_adopted(pilot):
    assert "interest-rate-adopt" in _ids(pilot)
    item = next(t for t in todos.collect(pilot) if t.id == "interest-rate-adopt")
    assert "RCW 64.34.364(13)" in item.sub and "12" in item.sub
    policy = LateFeePolicy.objects.filter(active=True).first()
    policy.interest_adopted_on = dt.date(2026, 10, 1)
    policy.interest_resolution = "Board resolution 2026-07"
    policy.save()
    assert "interest-rate-adopt" not in _ids(pilot)


def test_not_asked_under_the_generic_pack(db):
    from homeoa.billing.services.latefees import todo_items
    from homeoa.core.models import Association

    assert todo_items(Association.objects.create(name="Other", short_name="OT")) == []
