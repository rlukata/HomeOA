"""The standard association setup sections, as plain policy questions.

Packs call :func:`standard_sections` and adjust it per question ``target``: a different
suggestion or set of choices, the citation behind it, a note. Nothing here comes from a
statute; the generic pack uses it as is.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from django.utils.text import format_lazy
from django.utils.translation import gettext_lazy as _

from homeoa.rules.base import SetupChoice as C
from homeoa.rules.base import SetupQuestion as Q
from homeoa.rules.base import SetupSection


def _money(*amounts, none_label=None):
    out = []
    for a in amounts:
        a = Decimal(a)
        if a == 0 and none_label is not None:
            out.append(C(value=a, label=none_label))
        else:
            out.append(C(value=a, label=f"${a:,.2f}"))
    return tuple(out)


def _days(*days):
    return tuple(C(value=d, label=format_lazy(_("{n} days"), n=d)) for d in days)


def _base() -> tuple[SetupSection, ...]:
    return (
        SetupSection(
            key="money",
            title=_("Late fees and payments"),
            intro=_("Late fee, interest, the order payments are applied in, and reminders."),
            questions=(
                Q(
                    target="late_fees.flat_amount",
                    label=_("Late fee for an assessment paid late"),
                    kind="money",
                    choices=_money("0", "10", "25", none_label=_("No late fee")),
                    suggested=Decimal("25"),
                    other=True,
                ),
                Q(
                    target="late_fees.grace_days",
                    label=_("Days after the due date before the late fee"),
                    kind="days",
                    choices=_days(10, 15),
                    suggested=10,
                    other=True,
                ),
                Q(
                    target="late_fees.interest_apr",
                    label=_("Interest on late assessments (% a year)"),
                    kind="percent",
                    choices=(
                        C(value=Decimal("0"), label=_("No interest (0%)")),
                        C(value=Decimal("6"), label="6%"),
                        C(value=Decimal("12"), label="12%"),
                    ),
                    suggested=Decimal("0"),
                    other=True,
                ),
                Q(
                    target="payments.allocation",
                    label=_("What a payment pays first"),
                    kind="choice",
                    choices=(
                        C(
                            value="assessments_first",
                            label=_("Assessments, then late fees and interest, then other"),
                            explain=_("Keeps the owner current on dues."),
                        ),
                        C(
                            value="fees_first",
                            label=_("Late fees and interest, then assessments, then other"),
                        ),
                        C(value="oldest_first", label=_("The oldest charge first, any type")),
                    ),
                    suggested="assessments_first",
                ),
                Q(
                    target="rule:delinquency.reminder_days",
                    label=_("Friendly payment reminder"),
                    kind="choice",
                    choices=(
                        C(value=None, label=_("No separate reminder")),
                        C(value=15, label=_("15 days after the due date")),
                    ),
                    suggested=15,
                ),
            ),
        ),
        SetupSection(
            key="enforcement",
            module="violations",
            title=_("Fines and enforcement"),
            intro=_(
                "Steps before a fine and the fine schedule. The schedule must be sent to "
                "owners before any fine."
            ),
            questions=(
                Q(
                    target="rule:enforcement.courtesy_notice_required",
                    label=_("A courtesy notice before the formal notice"),
                    kind="choice",
                    choices=(C(value=True, label=_("Yes")), C(value=False, label=_("No"))),
                    suggested=True,
                ),
                Q(
                    target="rule:enforcement.cure_days",
                    label=_("Days the owner has to fix it after the formal notice"),
                    kind="days",
                    choices=_days(14, 30),
                    suggested=14,
                    other=True,
                ),
                Q(
                    target="rule:enforcement.hearing_notice_min_days",
                    label=_("Notice of a hearing, at least"),
                    kind="days",
                    choices=_days(10, 14),
                    suggested=10,
                    other=True,
                ),
                Q(
                    target="fines.first_amount",
                    label=_("Fine for a first violation"),
                    kind="money",
                    choices=_money("25", "50", "100"),
                    suggested=Decimal("50"),
                    other=True,
                ),
                Q(
                    target="fines.repeat_amount",
                    label=_("Fine for a repeat violation"),
                    kind="money",
                    choices=_money("50", "100", "200"),
                    suggested=Decimal("100"),
                    other=True,
                ),
                Q(
                    target="fines.continuing_amount",
                    label=_("Fine while a violation continues, per period"),
                    kind="money",
                    choices=_money("0", "10", "25", none_label=_("None")),
                    suggested=Decimal("0"),
                    other=True,
                ),
                Q(
                    target="fines.continuing_days",
                    label=_("Length of that period"),
                    kind="days",
                    choices=_days(7, 30),
                    suggested=7,
                    other=True,
                ),
            ),
        ),
        SetupSection(
            key="approvals",
            title=_("Approvals and limits"),
            intro=_("How many board members approve a bill, and spending limits."),
            questions=(
                Q(
                    target="bills.two_approvals_from",
                    label=_("Bills that need two board approvals"),
                    kind="money",
                    choices=(
                        C(value=Decimal("0"), label=_("Every bill")),
                        C(value=Decimal("500"), label=_("$500 or more")),
                        C(value=Decimal("1000"), label=_("$1,000 or more")),
                    ),
                    suggested=Decimal("1000"),
                    other=True,
                    help=_("Smaller bills need one approval."),
                ),
                Q(
                    target="maintenance.oneoff_limit",
                    module="work_orders",
                    label=_("One-off maintenance jobs the board may order without a vote, up to"),
                    kind="money",
                    choices=_money("500", "1000", "2500"),
                    suggested=Decimal("1000"),
                    other=True,
                ),
                Q(
                    target="rule:arc.decision_days",
                    module="arc",
                    label=_("Days to decide an architectural request"),
                    kind="days",
                    choices=_days(30, 45, 60),
                    suggested=30,
                    other=True,
                ),
            ),
        ),
        SetupSection(
            key="records",
            title=_("Records, meetings and notices"),
            intro=_(
                "Records requests, the owner meeting quorum, resale certificates and e-notice."
            ),
            questions=(
                Q(
                    target="rule:records.inspection_days",
                    label=_("Records available to an owner within"),
                    kind="days",
                    choices=_days(10, 14),
                    suggested=10,
                    other=True,
                ),
                Q(
                    target="rule:records.inspection_max_days",
                    label=_("At the latest, if the association needs more time"),
                    kind="days",
                    choices=_days(21, 30),
                    suggested=21,
                    other=True,
                ),
                Q(
                    target="rule:meetings.owner.quorum_percent",
                    label=_("Owner meeting quorum (% of votes)"),
                    kind="percent",
                    choices=(
                        C(value=Decimal("25"), label="25%"),
                        C(value=Decimal("50"), label="50%"),
                    ),
                    suggested=Decimal("25"),
                    other=True,
                    help=_("Use the number in your bylaws if they set one."),
                ),
                Q(
                    target="resale.fee",
                    label=_("Fee for a resale certificate"),
                    kind="money",
                    choices=_money("0", "100", "200", none_label=_("No fee")),
                    suggested=Decimal("100"),
                    other=True,
                ),
                Q(
                    target="consent.adopt",
                    label=_("Electronic notice consent form"),
                    kind="choice",
                    choices=(
                        C(
                            value=True,
                            label=_("Adopt the consent form owners see in HomeOA"),
                            explain=_("Owners who sign it get notices by email."),
                        ),
                        C(value=False, label=_("Not now: notices go by mail")),
                    ),
                    suggested=True,
                ),
            ),
        ),
    )


def standard_sections(adjust: dict[str, dict] | None = None) -> tuple[SetupSection, ...]:
    """The standard sections, with ``adjust[target]`` fields replaced on that question."""
    adjust = adjust or {}
    out = []
    for section in _base():
        questions = tuple(
            dataclasses.replace(q, **adjust[q.target]) if q.target in adjust else q
            for q in section.questions
        )
        out.append(dataclasses.replace(section, questions=questions))
    return tuple(out)
