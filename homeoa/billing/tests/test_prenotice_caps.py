# ruff: noqa: F811  (pytest fixtures imported from test_statutory_notices)
"""Late fees before the first notice are capped (RCW 64.34.364(17)(b) shaped).

The pack values here mirror the WA pack's (unverified) numbers; nothing is a statement of law.
"""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from homeoa import rules
from homeoa.billing.models import Charge, LateFee, LateFeePolicy
from homeoa.billing.services import latefees
from homeoa.registry.models import Unit
from homeoa.statements import delinquency
from homeoa.statements.tests.test_statutory_notices import TEST_PACK, _v, assoc  # noqa: F401

D = Decimal


def _pack(monkeypatch, **extra):
    values = {**TEST_PACK.values, **{k: _v(v) for k, v in extra.items()}}
    pack = dataclasses.replace(TEST_PACK, values=values)
    monkeypatch.setattr(rules, "active_pack", lambda association: pack)


@pytest.fixture
def pack(monkeypatch):
    _pack(
        monkeypatch,
        **{
            "late_fees.pre_notice.max_amount": "50.00",
            "late_fees.pre_notice.max_percent": "5",
            "late_fees.pre_notice.admin_fee_max": "10.00",
            "interest.annual_rate_max": "12",
        },
    )


def _policy(assoc, **kw):
    fields = {
        "association": assoc,
        "effective_start": dt.date(2026, 1, 1),
        **latefees.default_policy_kwargs(assoc),
        "flat_amount": D("100.00"),
        "grace_days": 10,
    }
    fields.update(kw)
    return LateFeePolicy.objects.create(**fields)


def _fees(kind="late_fee"):
    return list(
        LateFee.objects.filter(kind=kind, source_charge__unit__number="G").order_by("period")
    )


def test_fee_clamped_to_percent_and_one_fee_in_window(assoc):
    p = _policy(assoc)
    latefees.assess(p, dt.date(2026, 9, 20))
    (fee,) = _fees()
    # G owes $350.00: 5% is $17.50, below the $50 cap.
    assert fee.charge.amount == D("17.50")
    assert fee.computed == D("100.00") and fee.capped
    assert "limited to $17.50 before the first notice" in fee.charge.description
    assert "limited to" in fee.charge.entry.memo
    # October's charge is late too, but the window allows one fee only.
    latefees.assess(p, dt.date(2026, 10, 20))
    assert len(_fees()) == 1


def test_fee_clamped_to_amount(assoc, monkeypatch):
    _pack(monkeypatch, **{"late_fees.pre_notice.max_amount": "50.00"})
    latefees.assess(_policy(assoc), dt.date(2026, 9, 20))
    (fee,) = _fees()
    assert fee.charge.amount == D("50.00")


def test_pilot_dues_five_percent():
    """Pilot unit A dues $396.63: 5% is $19.83."""
    assert latefees.pre_notice_limit({"amount": "50.00", "percent": "5"}, D("396.63")) == D("19.83")
    assert latefees.pre_notice_limit({"amount": "50.00", "percent": None}, D("396.63")) == D(
        "50.00"
    )


def test_waived_fee_does_not_use_up_the_window(assoc):
    p = _policy(assoc)
    latefees.assess(p, dt.date(2026, 9, 20))
    latefees.waive(_fees()[0], reason="courtesy")
    latefees.assess(p, dt.date(2026, 10, 20))
    assert len(_fees()) == 2


def test_after_standstill_policy_applies_in_full(assoc):
    p = _policy(assoc)
    latefees.assess(p, dt.date(2026, 9, 20))
    # First notice drafted Sep 11; standstill (15 days) ends Sep 26.
    delinquency.run(assoc, dt.date(2026, 9, 11))
    latefees.assess(p, dt.date(2026, 10, 20))
    fees = _fees()
    assert [f.charge.amount for f in fees] == [D("17.50"), D("100.00")]
    assert not fees[1].capped


def test_no_caps_without_pack_keys(assoc, monkeypatch):
    """Without pre-notice keys the policy applies as configured."""
    _pack(monkeypatch)
    p = _policy(assoc)
    latefees.assess(p, dt.date(2026, 9, 20))
    latefees.assess(p, dt.date(2026, 10, 20))
    assert [f.charge.amount for f in _fees()] == [D("100.00"), D("100.00")]


def test_interest_clamped_to_max_rate(assoc):
    p = _policy(assoc, flat_amount=D("0"), interest_apr=D("18"))
    latefees.assess(p, dt.date(2026, 11, 1))
    fees = _fees("interest")
    sep = next(f for f in fees if f.source_charge.due_date == dt.date(2026, 9, 10))
    # October, 31 days at 12% (not 18%) on $350.00.
    assert sep.charge.amount == latefees.interest_amount(D("350.00"), D("12"), 31)
    assert sep.computed == latefees.interest_amount(D("350.00"), D("18"), 31)
    assert sep.capped
    assert "rate limited to 12% a year" in sep.charge.description
    assert Charge.objects.filter(unit=Unit.objects.get(number="G"), kind="interest").exists()
