"""Rule matching. Pure lookups, no writes."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm, roles_for

from .models import ApprovalRule

DEFAULT_REQUIRED = 1


@dataclass(frozen=True)
class Requirement:
    """What a bill needs: ``rule`` is ``None`` for the built-in default (one approval by
    a board member with ``bills.approve``; the person who entered it may not approve)."""

    rule: ApprovalRule | None
    required: int
    roles: frozenset[str]
    allow_submitter: bool


DEFAULT = Requirement(None, DEFAULT_REQUIRED, frozenset({perms.BOARD}), False)


def matches(rule: ApprovalRule, amount: Decimal, vendor) -> bool:
    if amount < rule.min_amount:
        return False
    if rule.max_amount is not None and amount >= rule.max_amount:
        return False
    if rule.vendor_id and rule.vendor_id != getattr(vendor, "pk", None):
        return False
    if rule.category:
        category = (getattr(vendor, "category", "") or "").strip().casefold()
        if category != rule.category.strip().casefold():
            return False
    return True


def requirement_for(bill) -> Requirement:
    amount = bill.total
    found = DEFAULT
    rules = ApprovalRule.all_associations.filter(association=bill.association_id, active=True)
    for rule in rules.order_by("priority", "pk"):
        if matches(rule, amount, bill.vendor):
            found = Requirement(rule, rule.required_approvals, rule.roles, rule.allow_submitter)
            break
    reserve = reserve_min_approvals(bill)
    if reserve and found.required < reserve:
        return dataclasses.replace(found, required=reserve)
    return found


def reserve_min_approvals(bill) -> int:
    """At least this many approvals when any line is paid from a reserve fund (rule key
    ``reserves.spend_min_approvals``; WA: two signatures, RCW 64.34.372(2)). 0 = no rule."""
    from homeoa import rules as rule_values

    if not bill.pk or not bill.lines.filter(fund__is_reserve=True).exists():
        return 0
    return int(rule_values.value(bill.association, "reserves.spend_min_approvals") or 0)


def is_eligible(user, bill, req: Requirement) -> bool:
    """May ``user`` approve or reject ``bill`` under ``req``? (Submitter rule aside.)"""
    if user is None or not getattr(user, "pk", None):
        return False
    if not has_perm(user, perms.BILLS_APPROVE, association=bill.association):
        return False
    return bool(roles_for(user, bill.association) & req.roles)


def is_submitter(user, bill) -> bool:
    return bill.created_by_id is not None and bill.created_by_id == user.pk
