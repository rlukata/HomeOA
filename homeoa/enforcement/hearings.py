"""Violation hearings and fines (VIO-04, VIO-05).

* A hearing is scheduled after the formal notice, at least
  ``enforcement.hearing_notice_min_days`` after its notice is sent (rule pack; Washington
  value unverified). The notice is a letter delivered like the other rungs.
* After the hearing date the board records attendance, the owner's statement and the
  decision: dismissed, cured without a fine, or a fine.
* A fine is refused unless: the hearing notice was sent and the hearing date has come; the
  formal notice's cure deadline has passed; and the fine schedule in force on the violation
  date was furnished to owners on or before that date (hard block, no override).
* The amount defaults from the schedule line (first offense, or repeat when the unit was
  already fined for the same category); a different amount needs a reason.
* The fine posts as a ``FINE`` charge on the owner ledger in the operating fund through
  ``billing.services.adjustments.manual_charge`` (Dr 1100 / Cr 4150 Fine income). It is
  paid in the "other charges" bucket and never accrues late fees or interest
. Voiding goes through ``void_charge``.
* The decision letter goes out as a ``DECISION`` step.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.utils.formats import date_format, time_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop as N_

from homeoa import audit, rules

from . import ladder
from .models import Hearing, Violation, ViolationStep
from .services import EnforcementError, schedule_in_force, set_status

FINE_INCOME = ("4150", N_("Fine income"))
HEARING = ViolationStep.Kind.HEARING_NOTICE
DECISION = ViolationStep.Kind.DECISION
CANCELLED = ViolationStep.Kind.HEARING_CANCELLED


def _today(association, today):
    from homeoa.core.context import association_today

    return today or association_today(association)


def notice_min_days(association, on: dt.date):
    return rules.resolve(association, "enforcement.hearing_notice_min_days", on)


def fine_income_account(association):
    from homeoa.ledger.models import Account, AccountType

    code, name = FINE_INCOME
    account, _created = Account.all_associations.get_or_create(
        association=association, code=code, defaults={"name": name, "type": AccountType.INCOME}
    )
    return account


def pending_hearing(violation: Violation) -> Hearing | None:
    return violation.hearings.filter(outcome="", cancelled_on__isnull=True).first()


# --- scheduling -------------------------------------------------------------------------


def schedule_problem(violation: Violation, hearing_on: dt.date | None, today: dt.date) -> str:
    """Why a hearing cannot be scheduled for ``hearing_on`` ("" when it can)."""
    if violation.status != Violation.Status.OPEN:
        return _("Only an open violation can go to a hearing.")
    if ladder.formal_step(violation) is None:
        return _("Send the formal notice first.")
    if pending_hearing(violation) is not None:
        return _("A hearing is already scheduled.")
    return _date_problem(violation, hearing_on, today)


def _date_problem(violation: Violation, hearing_on: dt.date | None, today: dt.date) -> str:
    days = notice_min_days(violation.association, today).value
    if days is None:
        return _("Set the hearing notice period (enforcement.hearing_notice_min_days) first.")
    if hearing_on is not None and hearing_on < today + dt.timedelta(days=days):
        return _(
            "The hearing must be at least %(n)s days after the notice is sent (earliest %(date)s)."
        ) % {"n": days, "date": (today + dt.timedelta(days=days)).isoformat()}
    return ""


def _hearing_text(h: Hearing) -> str:
    text = date_format(h.hearing_on, "F j, Y")
    if h.hearing_time:
        text += ", " + time_format(h.hearing_time)
    return text


def _extra(violation: Violation, hearing: Hearing) -> dict:
    from homeoa.letters.merge import money

    cure_by = getattr(ladder.formal_step(violation), "cure_by", None)
    extra = ladder.merge_extra(violation, cure_by)
    extra.update(
        violation_hearing_on=_hearing_text(hearing),
        violation_hearing_place=(
            _("online: %(link)s") % {"link": hearing.place} if hearing.online else hearing.place
        ),
        violation_decision=hearing.get_outcome_display() if hearing.outcome else "",
        violation_fine_imposed=money(hearing.fine_amount) if hearing.fine_amount else "",
    )
    return extra


def _lock(violation: Violation) -> Violation:
    return (
        Violation.all_associations.select_for_update()
        .select_related("unit", "category", "association")
        .get(pk=violation.pk)
    )


@transaction.atomic
def schedule_hearing(
    violation: Violation,
    *,
    hearing_on: dt.date,
    place: str,
    hearing_time: dt.time | None = None,
    online: bool = False,
    user=None,
    today=None,
) -> Hearing:
    violation = _lock(violation)
    association = violation.association
    today = _today(association, today)
    place = (place or "").strip()[:300]
    if not place:
        raise EnforcementError(_("Enter the hearing place or online link."))
    problem = schedule_problem(violation, hearing_on, today)
    if problem:
        raise EnforcementError(problem)
    hearing = Hearing(
        association=association,
        violation=violation,
        hearing_on=hearing_on,
        hearing_time=hearing_time,
        place=place,
        online=online,
    )
    step = ladder.deliver_step(
        violation,
        HEARING,
        extra=_extra(violation, hearing),
        today=today,
        user=user,
        period=f"violation:{violation.pk}:{HEARING}:{hearing_on.isoformat()}",
    )
    hearing.notice_step = step
    hearing.save()
    audit.record(
        "enforcement.hearing_scheduled",
        violation,
        actor=user,
        changes={
            "hearing": hearing.pk,
            "hearing_on": hearing_on.isoformat(),
            "place": place,
            "online": online,
            "notice_sent_on": today.isoformat(),
        },
    )
    return hearing


def _pending_locked(hearing: Hearing) -> tuple[Violation, Hearing]:
    violation = _lock(hearing.violation)
    hearing = Hearing.all_associations.select_for_update().get(pk=hearing.pk)
    if hearing.outcome:
        raise EnforcementError(_("This hearing already has a decision."))
    if hearing.cancelled:
        raise EnforcementError(_("This hearing was cancelled."))
    if violation.status != Violation.Status.OPEN:
        raise EnforcementError(_("Only an open violation can go to a hearing."))
    return violation, hearing


@transaction.atomic
def reschedule_hearing(
    hearing: Hearing,
    *,
    hearing_on: dt.date,
    place: str,
    hearing_time: dt.time | None = None,
    online: bool = False,
    user=None,
    today=None,
) -> Hearing:
    """Move a pending hearing and send an updated hearing notice. The new date must again be
    at least ``enforcement.hearing_notice_min_days`` after the updated notice is sent."""
    violation, hearing = _pending_locked(hearing)
    today = _today(violation.association, today)
    place = (place or "").strip()[:300]
    if not place:
        raise EnforcementError(_("Enter the hearing place or online link."))
    problem = _date_problem(violation, hearing_on, today)
    if problem:
        raise EnforcementError(problem)
    before = {
        "hearing_on": hearing.hearing_on.isoformat(),
        "hearing_time": hearing.hearing_time.isoformat() if hearing.hearing_time else None,
        "place": hearing.place,
        "online": hearing.online,
        "notice_step": hearing.notice_step_id,
    }
    hearing.hearing_on = hearing_on
    hearing.hearing_time = hearing_time
    hearing.place = place
    hearing.online = online
    sent = violation.steps.filter(kind=HEARING).count()
    step = ladder.deliver_step(
        violation,
        HEARING,
        extra=_extra(violation, hearing),
        today=today,
        user=user,
        # Notification period key (max 50 chars): one per updated notice.
        period=f"violation:{violation.pk}:hr:{hearing.pk}:{sent + 1}",
    )
    hearing.notice_step = step
    hearing.save()
    audit.record(
        "enforcement.hearing_rescheduled",
        violation,
        actor=user,
        changes={
            "hearing": hearing.pk,
            "before": before,
            "hearing_on": hearing_on.isoformat(),
            "hearing_time": hearing_time.isoformat() if hearing_time else None,
            "place": place,
            "online": online,
            "notice_step": step.pk,
            "notice_sent_on": today.isoformat(),
        },
    )
    return hearing


@transaction.atomic
def cancel_hearing(hearing: Hearing, reason: str = "", *, user=None, today=None) -> Hearing:
    """Cancel a pending hearing and send the owner a "hearing cancelled" letter (delivered
    like the hearing notice; emails after commit). A new hearing can then be
    scheduled."""
    violation, hearing = _pending_locked(hearing)
    today = _today(violation.association, today)
    hearing.cancelled_on = today
    hearing.cancel_reason = (reason or "").strip()[:300]
    hearing.save(update_fields=["cancelled_on", "cancel_reason"])
    extra = _extra(violation, hearing)
    extra["violation_cancel_reason"] = hearing.cancel_reason
    step = ladder.deliver_step(
        violation,
        CANCELLED,
        extra=extra,
        today=today,
        user=user,
        # Notification period key (max 50 chars).
        period=f"violation:{violation.pk}:hc:{hearing.pk}",
    )
    audit.record(
        "enforcement.hearing_cancelled",
        violation,
        actor=user,
        changes={
            "hearing": hearing.pk,
            "hearing_on": hearing.hearing_on.isoformat(),
            "letter_step": step.pk,
        },
        message=hearing.cancel_reason,
    )
    return hearing


# --- fines ------------------------------------------------------------------------------


def is_repeat(violation: Violation) -> bool:
    """Was the unit already fined (not voided) for this category before?"""
    return (
        Hearing.all_associations.filter(
            association=violation.association,
            violation__unit=violation.unit,
            violation__category=violation.category,
            violation__observed_on__lte=violation.observed_on,
            outcome=Hearing.Outcome.FINE,
            charge__void_entry__isnull=True,
        )
        .exclude(violation=violation)
        .exists()
    )


def schedule_amount(violation: Violation) -> Decimal | None:
    line = ladder.fine_line(violation)
    if line is None:
        return None
    return line.repeat_amount if is_repeat(violation) else line.first_amount


def fine_problem(hearing: Hearing, today: dt.date) -> str:
    """Why a fine cannot be imposed after ``hearing`` on ``today`` ("" when it can)."""
    violation = hearing.violation
    if hearing.notice_step_id is None:
        return _("No fine without a hearing notice.")
    if hearing.hearing_on > today:
        return _("No fine before the hearing: it is on %(date)s.") % {
            "date": hearing.hearing_on.isoformat()
        }
    formal = ladder.formal_step(violation)
    if formal is None or formal.cure_by is None:
        return _("No fine without a formal notice and cure deadline.")
    if formal.cure_by >= today:
        return _("No fine before the cure deadline (%(date)s) has passed.") % {
            "date": formal.cure_by.isoformat()
        }
    schedule = schedule_in_force(violation.association, violation.observed_on)
    if schedule is None:
        return _("No fine schedule was in force on the violation date, so no fine can be imposed.")
    if not schedule.furnished_by(violation.observed_on):
        return _(
            "%(schedule)s was not furnished to owners on or before the violation date "
            "(%(date)s), so no fine can be imposed."
        ) % {"schedule": schedule, "date": violation.observed_on.isoformat()}
    if ladder.fine_line(violation) is None:
        return _("%(schedule)s has no fine for %(category)s.") % {
            "schedule": schedule,
            "category": violation.category,
        }
    return ""


def _money(value) -> Decimal:
    amount = Decimal(value).quantize(Decimal("0.01"))
    if amount <= 0:
        raise EnforcementError(_("The fine must be more than zero."))
    return amount


def _post_fine(hearing: Hearing, violation: Violation, today: dt.date, user):
    from homeoa.billing.models import ChargeKind
    from homeoa.billing.services import adjustments
    from homeoa.payments.services import PaymentError

    description = _("Violation fine: %(category)s (observed %(date)s)") % {
        "category": violation.category,
        "date": violation.observed_on.isoformat(),
    }
    try:
        return adjustments.manual_charge(
            violation.unit,
            hearing.fine_amount,
            _("Hearing decision %(date)s") % {"date": today.isoformat()},
            description,
            kind=ChargeKind.FINE,
            account=fine_income_account(violation.association),
            date=today,
            due_date=today,
            posted_by=user,
        )
    except PaymentError as exc:
        raise EnforcementError(str(exc)) from exc


@transaction.atomic
def record_decision(
    hearing: Hearing,
    *,
    outcome: str,
    attended: bool | None = None,
    owner_statement: str = "",
    note: str = "",
    fine_amount=None,
    amount_reason: str = "",
    user=None,
    today=None,
) -> Hearing:
    violation = _lock(hearing.violation)
    hearing = Hearing.all_associations.select_for_update().get(pk=hearing.pk)
    association = violation.association
    today = _today(association, today)
    if hearing.outcome:
        raise EnforcementError(_("This hearing already has a decision."))
    if hearing.cancelled:
        raise EnforcementError(_("This hearing was cancelled."))
    if outcome not in Hearing.Outcome.values:
        raise EnforcementError(_("Choose a decision."))
    if violation.status != Violation.Status.OPEN:
        raise EnforcementError(_("Only an open violation can be decided."))
    if hearing.hearing_on > today:
        raise EnforcementError(
            _("Record the decision after the hearing (%(date)s).")
            % {"date": hearing.hearing_on.isoformat()}
        )
    hearing.attended = attended
    hearing.owner_statement = (owner_statement or "").strip()
    hearing.decision_note = (note or "").strip()
    hearing.outcome = outcome
    hearing.decided_on = today
    hearing.decided_by = user if getattr(user, "is_authenticated", False) else None
    if outcome == Hearing.Outcome.FINE:
        problem = fine_problem(hearing, today)
        if problem:
            raise EnforcementError(problem)
        default = schedule_amount(violation)
        amount = default if fine_amount in (None, "") else _money(fine_amount)
        amount_reason = (amount_reason or "").strip()[:300]
        if amount != default and not amount_reason:
            raise EnforcementError(
                _("Give a reason for a fine different from the schedule amount (%(amt)s).")
                % {"amt": default}
            )
        hearing.schedule_amount = default
        hearing.fine_amount = amount
        hearing.amount_reason = amount_reason if amount != default else ""
        hearing.charge = _post_fine(hearing, violation, today, user)
        set_status(violation, Violation.Status.CLOSED, note=_("Fined after hearing"), user=user)
    elif outcome == Hearing.Outcome.DISMISSED:
        set_status(violation, Violation.Status.DISMISSED, note=_("Dismissed at hearing"), user=user)
    else:
        set_status(violation, Violation.Status.CURED, note=_("Cured, no fine"), user=user)
        if violation.cured_on is None:
            violation.cured_on = today
            violation.save(update_fields=["cured_on"])
    hearing.save()
    step = ladder.deliver_step(
        violation,
        DECISION,
        extra=_extra(violation, hearing),
        today=today,
        user=user,
        period=f"violation:{violation.pk}:{DECISION}:{hearing.pk}",
    )
    hearing.decision_step = step
    hearing.save(update_fields=["decision_step"])
    audit.record(
        "enforcement.hearing_decided",
        violation,
        actor=user,
        changes={
            "hearing": hearing.pk,
            "outcome": outcome,
            "attended": attended,
            "fine_amount": str(hearing.fine_amount) if hearing.fine_amount else None,
            "schedule_amount": str(hearing.schedule_amount) if hearing.schedule_amount else None,
            "charge": hearing.charge_id,
            "letter": step.letter_id,
        },
        message=hearing.amount_reason,
    )
    return hearing


@transaction.atomic
def void_fine(hearing: Hearing, reason: str, *, user=None, today=None):
    from homeoa.billing.services import adjustments
    from homeoa.payments.services import PaymentError

    hearing = Hearing.all_associations.select_for_update().get(pk=hearing.pk)
    if hearing.charge_id is None:
        raise EnforcementError(_("This hearing imposed no fine."))
    reason = (reason or "").strip()
    if not reason:
        raise EnforcementError(_("Give a reason for voiding the fine."))
    try:
        adj = adjustments.void_charge(
            hearing.charge, reason, date=_today(hearing.association, today), posted_by=user
        )
    except PaymentError as exc:
        raise EnforcementError(str(exc)) from exc
    audit.record(
        "enforcement.fine_voided",
        hearing.violation,
        actor=user,
        changes={"hearing": hearing.pk, "charge": hearing.charge_id, "adjustment": adj.pk},
        message=reason[:300],
    )
    return adj
