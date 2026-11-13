"""A legal limit on a setup answer is one kind of constraint, checked when it is built."""

from decimal import Decimal

import pytest

from homeoa.rules.base import LegalLimit, RulePackError


def test_a_legal_limit_refuses_two_kinds_at_once():
    with pytest.raises(RulePackError):
        LegalLimit(fixed=True, maximum=Decimal("10"))
    with pytest.raises(RulePackError):
        LegalLimit(allowed=("a",), minimum=1)


def test_a_legal_limit_needs_one_kind():
    with pytest.raises(RulePackError):
        LegalLimit(citation="Test Act 1")


def test_a_legal_limit_refuses_a_minimum_above_its_maximum():
    with pytest.raises(RulePackError):
        LegalLimit(minimum=Decimal("20"), maximum=Decimal("10"))


def test_a_legal_bound_is_a_number_or_a_rule_reference():
    assert LegalLimit(maximum="rule:resale.fee_cap").is_range
    with pytest.raises(RulePackError):
        LegalLimit(maximum="275")


def test_a_law_may_fix_an_answer_to_false():
    assert LegalLimit(fixed=False).is_fixed
