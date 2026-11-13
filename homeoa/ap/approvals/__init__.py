"""Bill approvals (FIN-12): configurable rules engine.

``required(bill)`` returns the status a bill takes when submitted: ``"approved"`` when the
matching rule needs no approvals, otherwise ``"pending"``. With no rules configured a bill
needs one approval by a board member with ``bills.approve`` other than whoever entered it.

``approve`` / ``reject`` / ``progress`` / ``can_approve`` live in ``.services``; matching
lives in ``.rules``; models in ``.models``.
"""

from __future__ import annotations

APPROVED = "approved"
PENDING = "pending"


def required(bill) -> str:
    from .rules import requirement_for

    return APPROVED if requirement_for(bill).required <= 0 else PENDING


def approve(bill, user):
    from .services import approve as _approve

    return _approve(bill, user)


def reject(bill, user, reason: str):
    from .services import reject as _reject

    return _reject(bill, user, reason)
