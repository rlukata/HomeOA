"""The catalog of rule keys every pack may set. Packs may not invent keys outside it."""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from homeoa.letters.statutory import CHOICES as STATUTORY_TEXTS
from homeoa.resale.catalog import CHOICES as CERTIFICATE_FORMS

from .base import Kind, RuleKey

ALLOCATION_BUCKETS = ("assessment", "late_fee", "interest", "other")
ALLOCATION_MODES = ("by_type", "oldest_first")
FINE_STEPS = (
    "adopted_fine_schedule",
    "written_notice",
    "opportunity_to_be_heard",
    "written_decision",
)
NOTICE_KINDS = ("owner_meeting", "special_meeting", "board_meeting", "budget_ratification")
VOTE_KINDS = ("audit_waiver", "budget_rejection")
# Board vote needed to adopt an emergency special assessment without owner ratification
BOARD_VOTES = ("majority", "two_thirds", "unanimous")
# "gte" = at least the threshold, "gt" = more than it (from the statute wording)
COMPARISONS = ("gte", "gt")
DELINQUENCY_STEPS = ("reminder", "late_notice", "demand")

_keys: list[RuleKey] = [
    RuleKey(
        "payments.allocation_order",
        Kind.ORDER,
        _("Payment allocation order"),
        _("Which charges a payment pays first."),
        choices=ALLOCATION_BUCKETS,
    ),
    RuleKey("payments.oldest_first", Kind.BOOL, _("Pay oldest charges first within each type")),
    RuleKey(
        "payments.allocation_mode",
        Kind.CHOICE,
        _("Payment allocation mode"),
        _(
            "by_type: follow the allocation order by charge type; "
            "oldest_first: pay the oldest charge first whatever its type."
        ),
        choices=ALLOCATION_MODES,
    ),
    RuleKey(
        "payments.fee_pass_through_allowed",
        Kind.BOOL,
        _("Owners may be charged the card/ACH processing fee"),
        _(
            "When unset or off, the association absorbs processing fees. Passing a fee "
            "through also needs the board to opt in on the payments plugin."
        ),
    ),
    RuleKey("late_fees.enabled", Kind.BOOL, _("Charge late fees")),
    RuleKey("late_fees.amount", Kind.MONEY, _("Late fee (flat)")),
    RuleKey("late_fees.grace_days", Kind.DAYS, _("Late fee grace period (days)")),
    RuleKey("late_fees.cap", Kind.MONEY, _("Late fee cap")),
    RuleKey(
        "interest.annual_rate",
        Kind.PERCENT,
        _("Interest on overdue balances (% a year)"),
        cap_key="interest.annual_rate_max",
    ),
    RuleKey(
        "interest.annual_rate_max",
        Kind.PERCENT,
        _("Highest interest rate allowed (% a year)"),
        cap_key="interest.annual_rate_max",
    ),
    RuleKey(
        "fines.due_process_steps",
        Kind.STEPS,
        _("Steps before a fine"),
        choices=FINE_STEPS,
    ),
    RuleKey("audit.cpa_audit_revenue_threshold", Kind.MONEY, _("CPA audit required at revenue")),
    RuleKey(
        "tax.1120h.exempt_income_min_pct",
        Kind.PERCENT,
        _("Form 1120-H: exempt function income at least (% of gross income)"),
    ),
    RuleKey(
        "tax.1120h.expenditure_min_pct",
        Kind.PERCENT,
        _("Form 1120-H: spending on association property at least (% of expenditures)"),
    ),
    RuleKey("tax.1120h.specific_deduction", Kind.MONEY, _("Form 1120-H: specific deduction")),
    RuleKey("tax.1120h.rate", Kind.PERCENT, _("Form 1120-H: tax rate (%)")),
    RuleKey("resale.certificate_days", Kind.DAYS, _("Resale certificate deadline (days)")),
    RuleKey(
        "resale.certificate_form",
        Kind.CHOICE,
        _("Resale certificate form"),
        _("The items a resale certificate must answer (homeoa.resale.catalog)."),
        choices=CERTIFICATE_FORMS,
    ),
    RuleKey(
        "resale.fee_cap", Kind.MONEY, _("Resale certificate fee cap"), cap_key="resale.fee_cap"
    ),
    RuleKey(
        "resale.update_fee_cap",
        Kind.MONEY,
        _("Resale certificate update fee cap"),
        cap_key="resale.update_fee_cap",
    ),
    RuleKey(
        "resale.update_window_months",
        Kind.DAYS,
        _("Resale certificate update window (months)"),
        _("An update requested within this many months of the certificate."),
    ),
    RuleKey(
        "notices.electronic_requires_consent",
        Kind.BOOL,
        _("Electronic notice needs the owner's consent"),
    ),
    RuleKey("reserves.study_required", Kind.BOOL, _("Reserve study required")),
    RuleKey(
        "reserves.spend_min_approvals",
        Kind.COUNT,
        _("Board approvals needed to pay a bill from the reserve fund"),
        _("Not set: the bill's normal approval rule applies."),
    ),
    RuleKey(
        "interest.statutory_default_applies",
        Kind.BOOL,
        _("Without an adopted rate, the law charges its maximum interest"),
        _("When on, Home asks the board to adopt an interest rate (it may be 0%)."),
    ),
    RuleKey(
        "statements.unpaid_assessments_days",
        Kind.DAYS,
        _("Statement of unpaid assessments deadline (days)"),
        _("Counted from the written request of an owner or lender. Not set: no deadline."),
    ),
    # Delinquency notices and foreclosure (research 2026-10)
    RuleKey(
        "delinquency.first_notice.max_days_past_due",
        Kind.DAYS,
        _("First delinquency notice, latest (days past due)"),
    ),
    RuleKey(
        "delinquency.standstill_days",
        Kind.DAYS,
        _("Days after the first notice before further collection"),
    ),
    RuleKey(
        "delinquency.second_notice.min_days_past_due",
        Kind.DAYS,
        _("Second delinquency notice, earliest (days past due)"),
    ),
    RuleKey(
        "delinquency.second_notice.min_days_after_first",
        Kind.DAYS,
        _("Second delinquency notice, earliest (days after the first)"),
    ),
    RuleKey(
        "delinquency.foreclosure.min_months_assessments",
        Kind.DAYS,
        _("Foreclosure threshold: months of assessments owed"),
    ),
    RuleKey(
        "delinquency.foreclosure.min_amount",
        Kind.MONEY,
        _("Foreclosure threshold: minimum assessments owed"),
    ),
    RuleKey(
        "delinquency.foreclosure.min_days_after_threshold",
        Kind.DAYS,
        _("Foreclosure: days after the threshold is reached"),
    ),
    RuleKey(
        "delinquency.notice.mail_required",
        Kind.BOOL,
        _("Delinquency notices must go by first-class mail"),
    ),
    RuleKey(
        "delinquency.notice.email_copy_if_known",
        Kind.BOOL,
        _("Delinquency notices also go by email when known"),
    ),
    RuleKey(
        "delinquency.notice.statutory_text",
        Kind.CHOICE,
        _("Statutory text printed on delinquency notices"),
        _(
            "A mandated notice block, printed verbatim and locked. When set, the ladder "
            "sends the statutory first and second notices instead of the late notice."
        ),
        choices=STATUTORY_TEXTS,
    ),
    # Late fees before the first notice
    RuleKey(
        "late_fees.pre_notice.max_amount",
        Kind.MONEY,
        _("Late fee before the first notice: most allowed ($)"),
        cap_key="late_fees.pre_notice.max_amount",
    ),
    RuleKey(
        "late_fees.pre_notice.max_percent",
        Kind.PERCENT,
        _("Late fee before the first notice: most allowed (% of the assessment)"),
        cap_key="late_fees.pre_notice.max_percent",
    ),
    RuleKey(
        "late_fees.pre_notice.admin_fee_max",
        Kind.MONEY,
        _("Administrative fee before the first notice: most allowed"),
        cap_key="late_fees.pre_notice.admin_fee_max",
    ),
    RuleKey(
        "payments.free_method_required",
        Kind.BOOL,
        _("Owners must have a way to pay without a fee"),
    ),
    RuleKey(
        "autopay.notice_days",
        Kind.DAYS,
        _("Notice before a varying autopay debit (days)"),
        _(
            "The least number of days between the notice of an autopay amount and its "
            "debit date. The board's notice lead may not be shorter."
        ),
    ),
    # Electronic notice
    RuleKey(
        "notices.electronic.auto_revoke_after_failures",
        Kind.DAYS,
        _("Electronic notice consent ends after this many failed sends in a row"),
    ),
    RuleKey(
        "notices.electronic.prior_vendor_consent_reusable",
        Kind.BOOL,
        _("Consent given to a previous manager may be reused"),
    ),
    # Budget
    RuleKey(
        "budget.distribute_within_days",
        Kind.DAYS,
        _("Budget summary to owners within (days of adoption)"),
    ),
    RuleKey(
        "budget.ratification.min_days",
        Kind.DAYS,
        _("Budget ratification meeting, earliest (days after the budget is sent)"),
    ),
    RuleKey(
        "budget.ratification.max_days",
        Kind.DAYS,
        _("Budget ratification meeting, latest (days after the budget is sent)"),
    ),
    RuleKey(
        "budget.rejection_percent_of_all_votes",
        Kind.PERCENT,
        _("Budget is rejected by more than this share of all votes (%)"),
    ),
    RuleKey(
        "budget.rejection_comparison",
        Kind.CHOICE,
        _("Budget rejection: at least (gte) or more than (gt) the share"),
        _("From the statute wording. A majority means more than (gt)."),
        choices=COMPARISONS,
    ),
    RuleKey(
        "budget.ratification.quorum_required",
        Kind.BOOL,
        _("Budget ratification needs a quorum"),
    ),
    RuleKey(
        "special_assessments.ratification_required",
        Kind.BOOL,
        _("Special assessments need owner ratification, like the budget"),
    ),
    RuleKey(
        "special_assessments.emergency_board_vote",
        Kind.CHOICE,
        _("Emergency special assessment without ratification: board vote needed"),
        _("Not set: every special assessment follows the ratification rule."),
        choices=BOARD_VOTES,
    ),
    # Fines
    RuleKey(
        "fines.requires_prior_adopted_schedule",
        Kind.BOOL,
        _("Fines need a fine schedule adopted and sent beforehand"),
    ),
    RuleKey("fines.requires_notice", Kind.BOOL, _("Fines need notice to the owner")),
    RuleKey("fines.requires_hearing", Kind.BOOL, _("Fines need an opportunity to be heard")),
    # Violation escalation ladder
    RuleKey(
        "enforcement.courtesy_notice_required",
        Kind.BOOL,
        _("A courtesy notice comes before the formal notice"),
    ),
    RuleKey(
        "enforcement.cure_days",
        Kind.DAYS,
        _("Days the owner has to cure after a formal notice"),
    ),
    RuleKey(
        "enforcement.hearing_notice_min_days",
        Kind.DAYS,
        _("Hearing notice, at least (days before the hearing)"),
    ),
    # Records inspection
    RuleKey(
        "records.inspection_days",
        Kind.DAYS,
        _("Records available to an owner within (days of the request)"),
    ),
    RuleKey(
        "records.inspection_max_days",
        Kind.DAYS,
        _("Records available at the latest, if the association needs more time (days)"),
    ),
    # Meetings
    RuleKey(
        "meetings.owner.quorum_percent",
        Kind.PERCENT,
        _("Owner meeting quorum (% of votes)"),
    ),
    # Architectural review: association policy from the declaration
    RuleKey(
        "arc.decision_days",
        Kind.DAYS,
        _("Architectural requests: decide within (days)"),
        _(
            "Counted from the submission, or from the day the owner answered a request for "
            "more information."
        ),
    ),
    RuleKey(
        "arc.first_reminder_days",
        Kind.DAYS,
        _("Architectural requests: first reminder to reviewers (days before the deadline)"),
    ),
    RuleKey(
        "arc.final_reminder_days",
        Kind.DAYS,
        _("Architectural requests: last reminder to reviewers (days before the deadline)"),
    ),
    RuleKey(
        "arc.deemed_approved",
        Kind.BOOL,
        _("Architectural requests are deemed approved when not decided in time"),
        _("Shown as a note only: HomeOA never approves a request on its own."),
    ),
    RuleKey(
        "arc.ev_charger.deemed_approval_days",
        Kind.DAYS,
        _("EV charger requests: deemed approved unless denied in writing within (days)"),
        _(
            "Counted from receipt; days spent waiting for the owner's answer to a request "
            "for more information don't count. HomeOA records the approval on the next day."
        ),
    ),
    RuleKey(
        "arc.heat_pump.deemed_approval_days",
        Kind.DAYS,
        _("Heat pump requests: deemed approved unless denied in writing within (days)"),
        _(
            "Counted from receipt; days spent waiting for the owner's answer to a request "
            "for more information don't count. HomeOA records the approval on the next day."
        ),
    ),
    # Board meetings
    RuleKey(
        "notices.board_meeting.min_days",
        Kind.DAYS,
        _("Board meeting notice, latest (days before)"),
        _("Unless the meeting is in a schedule already given to owners."),
    ),
    RuleKey(
        "notices.board_meeting.emergency_min_days",
        Kind.DAYS,
        _("Board meeting notice for an unforeseeable event, latest (days before)"),
        _("Sent electronically."),
    ),
    RuleKey(
        "notices.board_meeting.emergency_to_known_addresses",
        Kind.BOOL,
        _("Emergency board meeting notice goes to every known email"),
        _("When off, only owners who consented to e-notice are emailed."),
    ),
    RuleKey(
        "meetings.board.owner_comment_minutes",
        Kind.MINUTES,
        _("Owner comment period at the start of board meetings (minutes)"),
        _("Listed first on every board meeting agenda. Not set: no fixed period."),
    ),
    RuleKey(
        "meetings.remote.phone_option_required",
        Kind.BOOL,
        _("Remote meetings must offer a way to join by telephone"),
    ),
    RuleKey(
        "meetings.remote.board_votes_by_roll_call",
        Kind.BOOL,
        _("Board votes at remote meetings are taken by roll call"),
    ),
    # Owner votes (read by votes/)
    RuleKey(
        "votes.ballot_retention_months",
        Kind.MONTHS,
        _("Keep ballots and proxies for at least (months)"),
        _("Ballots are never deleted automatically."),
    ),
    RuleKey(
        "votes.electronic.express_authority",
        Kind.BOOL,
        _("The statute expressly allows electronic ballots"),
        _(
            "When no, electronic ballots run only alongside paper ballots and the bylaws "
            "decide whether they are allowed."
        ),
    ),
    RuleKey(
        "votes.paper_ballot_for_nonconsenting",
        Kind.BOOL,
        _("Owners who have not consented to electronic voting get a paper ballot"),
    ),
]

_NOTICE_LABELS = {
    "owner_meeting": (
        _("Owner meeting notice, earliest (days before)"),
        _("Owner meeting notice, latest (days before)"),
    ),
    "special_meeting": (
        _("Special meeting notice, earliest (days before)"),
        _("Special meeting notice, latest (days before)"),
    ),
}
for _kind in NOTICE_KINDS:
    # budget_ratification: renamed (see RENAMED); board_meeting: explicit keys above
    if _kind not in _NOTICE_LABELS:
        continue
    _max_label, _min_label = _NOTICE_LABELS[_kind]
    _keys.append(RuleKey(f"notices.{_kind}.max_days", Kind.DAYS, _max_label))
    _keys.append(RuleKey(f"notices.{_kind}.min_days", Kind.DAYS, _min_label))

_VOTE_LABELS = {
    "audit_waiver": _("Votes needed to waive the annual audit (%)"),
    "budget_rejection": _("Votes needed to reject the budget (%)"),
}
_COMPARISON_LABELS = {
    "audit_waiver": _("Audit waiver: at least (gte) or more than (gt) the share"),
}
for _kind in VOTE_KINDS:
    if _kind == "budget_rejection":  # renamed (see RENAMED)
        continue
    _keys.append(RuleKey(f"votes.{_kind}.threshold_percent", Kind.PERCENT, _VOTE_LABELS[_kind]))
    _keys.append(
        RuleKey(
            f"votes.{_kind}.threshold_comparison",
            Kind.CHOICE,
            _COMPARISON_LABELS[_kind],
            _("From the statute wording. A majority means more than (gt)."),
            choices=COMPARISONS,
        )
    )

_DELINQUENCY_LABELS = {
    "reminder": _("Delinquency: payment reminder (days after due date)"),
    "late_notice": _("Delinquency: late notice (days after due date)"),
    "demand": _("Delinquency: demand letter (days after due date)"),
}
for _step in DELINQUENCY_STEPS:
    _keys.append(
        RuleKey(
            f"delinquency.{_step}_days",
            Kind.DAYS,
            _DELINQUENCY_LABELS[_step],
            _("Counted from the due date of the oldest unpaid charge. Not set: step is off."),
        )
    )

KEYS: dict[str, RuleKey] = {k.key: k for k in _keys}

# Keys the 2026-10 statute research renamed. Old names still resolve (packs, overrides
# saved under the old name, callers) to the new key; they are not listed in KEYS.
RENAMED: dict[str, str] = {
    "notices.budget_ratification.min_days": "budget.ratification.min_days",
    "notices.budget_ratification.max_days": "budget.ratification.max_days",
    "votes.budget_rejection.threshold_percent": "budget.rejection_percent_of_all_votes",
    "votes.budget_rejection.threshold_comparison": "budget.rejection_comparison",
}


def canonical(key: str) -> str:
    """The current name for ``key`` (follows ``RENAMED``)."""
    return RENAMED.get(key, key)


def comparison_key(threshold_key: str) -> str:
    """The companion comparison key of a vote threshold key."""
    key = canonical(threshold_key)
    if key == "budget.rejection_percent_of_all_votes":
        return "budget.rejection_comparison"
    return key.removesuffix("_percent") + "_comparison"
