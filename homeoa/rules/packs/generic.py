"""The generic rule pack: no statutory checks, every value board-configurable.

Defaults (payment allocation order, placeholder late-fee terms) are HomeOA's own
association-policy defaults, not statute. Everything else is unset
(``None``): the board decides, through an association override.
"""

from __future__ import annotations

from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from ..base import Basis, RulePack, RuleValue
from ..keys import KEYS
from .setup import standard_sections

ALLOCATION = "HomeOA default: payment allocation order, association policy, not statute"
AUTOPAY_NOTICE = "HomeOA default: 12 CFR 1005.10(d) (Regulation E, federal), policy default"
LATE_FEES = "HomeOA default: placeholder association policy, not statute"
MAJORITY = "HomeOA default: a majority means more than half, not statute"
MEETING_NOTICE = (
    "HomeOA default: placeholder meeting notice window, association policy, not statute; "
    "set it from the bylaws"
)
EMERGENCY_NOTICE = (
    "HomeOA default: placeholder emergency board meeting notice, association policy, "
    "not statute; set it from the bylaws"
)
ARC_DEADLINE = (
    "HomeOA default: placeholder architectural review deadline, association policy, not "
    "statute; set it from the declaration"
)
VIOLATION_NOTICE = (
    "HomeOA default: placeholder violation-notice policy, not statute; "
    "set it from the declaration, bylaws or rules"
)


def _policy(value, citation):
    return RuleValue(value=value, basis=Basis.POLICY, verified=False, citation=citation)


_set = {
    "payments.allocation_order": _policy(
        ("assessment", "late_fee", "interest", "other"), ALLOCATION
    ),
    "payments.oldest_first": _policy(True, ALLOCATION),
    "payments.allocation_mode": _policy("by_type", ALLOCATION),
    "resale.certificate_form": _policy(
        "generic", "HomeOA default: a short resale certificate checklist, not statute"
    ),
    "late_fees.enabled": _policy(True, LATE_FEES),
    "late_fees.amount": _policy(Decimal("25.00"), LATE_FEES),
    "late_fees.grace_days": _policy(10, LATE_FEES),
    "interest.annual_rate": _policy(Decimal("0"), LATE_FEES),
    "autopay.notice_days": _policy(10, AUTOPAY_NOTICE),
    # Meeting notice windows (days before the meeting). A jurisdiction pack replaces these.
    "notices.owner_meeting.min_days": _policy(10, MEETING_NOTICE),
    "notices.owner_meeting.max_days": _policy(60, MEETING_NOTICE),
    "notices.special_meeting.min_days": _policy(10, MEETING_NOTICE),
    "notices.special_meeting.max_days": _policy(60, MEETING_NOTICE),
    "notices.board_meeting.min_days": _policy(2, MEETING_NOTICE),
    "notices.board_meeting.emergency_min_days": _policy(1, EMERGENCY_NOTICE),
    # Architectural review deadline and reminders. Deemed approval: board sets it.
    "arc.decision_days": _policy(30, ARC_DEADLINE),
    "arc.first_reminder_days": _policy(7, ARC_DEADLINE),
    "arc.final_reminder_days": _policy(1, ARC_DEADLINE),
    # Threshold comparisons: anything called a majority is "more than".
    "votes.audit_waiver.threshold_comparison": _policy("gt", MAJORITY),
    "budget.rejection_comparison": _policy("gt", MAJORITY),
    # Violation escalation: association policy placeholders.
    "enforcement.courtesy_notice_required": _policy(True, VIOLATION_NOTICE),
    "enforcement.cure_days": _policy(14, VIOLATION_NOTICE),
    "enforcement.hearing_notice_min_days": _policy(10, VIOLATION_NOTICE),
}

values = {
    key: _set.get(key, RuleValue(value=None, basis=Basis.BOARD, verified=False)) for key in KEYS
}

pack = RulePack(
    id="generic",
    name=_("Generic (board-configurable)"),
    jurisdiction="",
    values=values,
    description=_("No statutory checks. The board sets every rule."),
    setup=standard_sections(),
)
