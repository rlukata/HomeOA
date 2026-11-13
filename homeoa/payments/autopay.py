"""Autopay (FIN-06, POR-02): owners let the association debit their bank each month.

Enrollment
----------
``enroll(unit, person, rule=..., mandate_ref=...)`` for the owner's current ownership
period. ``rule`` is ``balance`` (the period's balance after any payments still
processing; the default) or ``fixed`` (``fixed_amount`` each month, capped at the
balance at debit time). ``mandate_ref`` must be one of the
payment provider's chargeable ``saved_methods`` for that owner; the provider re-checks it
at each debit. Autopay starts with the first month after enrolling (``starts_on``), so an
owner who enrolls mid-month pays the current month by hand. One active enrollment per
ownership period; enrolling again replaces it; ``cancel`` turns it off. All audited.

The job
-------
``run(association, as_of)`` (periodic job ``payments.autopay``, which sorts after
``billing.post_assessments`` so the month's dues are posted first) handles each active
enrollment once per month (``AutopayCharge`` is unique per unit and month) in two steps:

* **Notice.** Each month's cycle has a debit date (``autopay.debit_day`` in
  ``Association.settings["autopay"]``, 1-28, default the dues due day) and a notice date
  (debit date - ``notice_lead_days``, default 15; never below the rule key
  ``autopay.notice_days``, 10). From the notice date, while at least ``notice_days``
  remain, the cycle is recorded ``scheduled`` with the noticed amount: the balance owed
  today plus the charges due by the debit date (``projected_amount``). A pre-debit notice
  (``payments.autopay_upcoming``, mandatory, consent routing) goes to every owner, unless a
  statement sent early enough that shows the debit date served (``notice_statement``), or
  a fixed amount that does not vary needs none. A cycle never noticed is never debited.
  Changing the debit day applies from the first cycle not yet due for notice
  (``set_cycle_settings``, audited); a noticed cycle keeps its debit date.
* **Debit.** On the noticed debit date the provider's ``collect`` debits
  min(noticed amount, balance after pending payments) and the charge is ``pending``; with
  nothing owed the month is ``skipped``. Money is recorded only when the provider
  reports success (payments posted through ``payments.services``, as for pay now).

A run that started at least one debit writes an ``AutopayRun`` ("Autopay collected from N
units") to the activity feed.

Failures (refused at once, or reported later through ``reference_failed`` by the
provider's webhook) mark the charge ``failed``, notify the owner (``payments.autopay_failed``;
``preview`` while residents are off) and the board (``payments.autopay_failed_board``), and
raise a board to-do until acknowledged. A failed month is never retried.

If the pre-debit notice itself fails (sending raised, reached nobody, or every delivery
failed), the cycle is recorded ``skipped_notice_failed`` and never debited; the
board gets an alert after commit and a to-do to resend and reschedule, or collect
manually. The owner was not at fault, so the late-fee job skips that unit's month, also
after a resend. ``resend_notice`` sends a fresh notice and moves the debit to no
earlier than ``notice_days`` after the resend; if the resend fails too the cycle stays
``skipped_notice_failed`` with a new board alert.

The residents switch gates everything: no enrollment while residents are off and
the job does nothing then.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit

from .models import (
    AutopayCharge,
    AutopayEnrollment,
    AutopayRule,
    AutopayRun,
    AutopayStatus,
    NoticeSource,
)
from .services import PaymentError, check_amount

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
OWNER_TYPE = "payments.autopay_failed"
BOARD_TYPE = "payments.autopay_failed_board"
UPCOMING_TYPE = "payments.autopay_upcoming"
NOTICE_FAILED_TYPE = "payments.autopay_notice_failed_board"


def register_notification_types() -> None:
    from homeoa.notify import channels as ch
    from homeoa.notify import types as ntypes

    ntypes.register(
        ntypes.NotificationType(
            module="online_payments",
            key=OWNER_TYPE,
            label=gettext_lazy("Autopay payment failed"),
            default_channels=(ch.INAPP, ch.EMAIL),
            audience=ntypes.AUDIENCE_RESIDENTS,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            module="online_payments",
            key=UPCOMING_TYPE,
            label=gettext_lazy("Upcoming autopay debit"),
            default_channels=(ch.EMAIL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            mandatory=True,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            module="online_payments",
            key=NOTICE_FAILED_TYPE,
            label=gettext_lazy("Autopay notice failed, debit skipped (board)"),
            default_channels=(ch.INAPP,),
            audience=ntypes.AUDIENCE_BOARD,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            module="online_payments",
            key=BOARD_TYPE,
            label=gettext_lazy("Autopay payment failed (board)"),
            default_channels=(ch.INAPP,),
            audience=ntypes.AUDIENCE_BOARD,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )


def month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def next_month(day: dt.date) -> dt.date:
    return (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)


def provider_for(association):
    from homeoa import providers

    return providers.get_provider("PaymentProvider", association)


def _owners(period):
    return [s.person for s in period.shares.select_related("person")]


def active_enrollment(period) -> AutopayEnrollment | None:
    if period is None:
        return None
    return AutopayEnrollment.all_associations.filter(
        ownership_period=period, cancelled_at__isnull=True
    ).first()


def enroll(
    unit,
    person,
    *,
    rule: str,
    mandate_ref: str,
    fixed_amount: Decimal | None = None,
    user=None,
    as_of: dt.date,
) -> AutopayEnrollment:
    from homeoa.core import residents_enabled
    from homeoa.registry.services import ownership_period_on

    association = unit.association
    if not residents_enabled(association):
        raise PaymentError(_("Autopay is not available yet."))
    period = ownership_period_on(unit, as_of)
    if period is None or person not in _owners(period):
        raise PaymentError(_("Only a current owner can set up autopay for this unit."))
    if rule not in AutopayRule.values:
        raise PaymentError(_("Choose how much autopay pays."))
    fixed_amount = check_amount(fixed_amount) if rule == AutopayRule.FIXED else None
    provider = provider_for(association)
    if not getattr(provider, "online", False):
        raise PaymentError(_("Online payments are not set up for this association."))
    method = next(
        (m for m in provider.saved_methods(unit, person) if m.ref == str(mandate_ref)), None
    )
    if method is None or not method.chargeable:
        raise PaymentError(_("Choose a verified bank account."))
    with transaction.atomic():
        old = active_enrollment(period)
        if old is not None:
            _cancel(old, user)
        enrollment = AutopayEnrollment.all_associations.create(
            association=association,
            unit=unit,
            ownership_period=period,
            person=person,
            rule=rule,
            fixed_amount=fixed_amount,
            mandate_ref=method.ref,
            mandate_label=str(method.label)[:120],
            starts_on=next_month(as_of),
            created_by=user if getattr(user, "pk", None) else None,
        )
        audit.record("payments.autopay_enrolled", enrollment, association=association)
    return enrollment


def _cancel(enrollment: AutopayEnrollment, user=None) -> None:
    enrollment.cancelled_at = timezone.now()
    enrollment.save(update_fields=["cancelled_at"])
    audit.record(
        "payments.autopay_cancelled",
        enrollment,
        association=enrollment.association,
        actor=user if getattr(user, "pk", None) else None,
    )


def cancel(enrollment: AutopayEnrollment, *, user=None) -> AutopayEnrollment:
    with transaction.atomic():
        enrollment = AutopayEnrollment.all_associations.select_for_update().get(pk=enrollment.pk)
        if enrollment.cancelled_at is None:
            _cancel(enrollment, user)
    return enrollment


# ------------------------------------------------------------------ debit day

SETTINGS_KEY = "autopay"
DEFAULT_DEBIT_DAY = 10
DEFAULT_LEAD_DAYS = 15
MAX_LEAD_DAYS = 45
MONTHS_AHEAD = 3  # a notice is at most MAX_LEAD_DAYS before its debit


class SettingsError(ValueError):
    """``str(err)`` is user-facing."""


def notice_days(association, as_of: dt.date | None = None) -> int:
    """The least notice before a varying debit (rule key ``autopay.notice_days``)."""
    from homeoa import rules

    value = rules.value(association, "autopay.notice_days", as_of)
    return int(value) if value is not None else 10


def _due_day(association, month: dt.date) -> int:
    from homeoa.billing.models import AssessmentSchedule

    schedules = AssessmentSchedule.all_associations.filter(
        association=association, active=True
    ).order_by("-effective_start")
    for schedule in schedules:
        if schedule.covers(month):
            return schedule.due_day
    return DEFAULT_DEBIT_DAY


def _history(association) -> list[dict]:
    block = (association.settings or {}).get(SETTINGS_KEY) or {}
    return sorted(block.get("history") or [], key=lambda e: e["from"])


def cycle_settings(association, month: dt.date) -> tuple[int, int]:
    """``(debit_day, notice_lead_days)`` in force for the cycle debiting in ``month``."""
    month = month_start(month)
    day, lead = None, DEFAULT_LEAD_DAYS
    for entry in _history(association):
        if dt.date.fromisoformat(entry["from"]) <= month:
            day, lead = entry["debit_day"], entry["notice_lead_days"]
    return (day or _due_day(association, month)), lead


def cycle_dates(association, month: dt.date) -> tuple[dt.date, dt.date]:
    """``(notice_on, debit_on)`` for the cycle debiting in ``month``."""
    day, lead = cycle_settings(association, month)
    debit_on = month_start(month).replace(day=day)
    return debit_on - dt.timedelta(days=lead), debit_on


def next_cycle(association, as_of: dt.date) -> tuple[dt.date, dt.date, dt.date]:
    """``(month, notice_on, debit_on)`` of the first cycle not yet due for notice."""
    month = month_start(as_of)
    while True:
        notice_on, debit_on = cycle_dates(association, month)
        if notice_on > as_of:
            return month, notice_on, debit_on
        month = next_month(month)


def set_cycle_settings(
    association, *, debit_day: int, notice_lead_days: int, as_of: dt.date, user=None
) -> dt.date:
    """Change the debit day and notice lead. Returns the first month it applies to.

    The change applies from the first cycle whose notice date is still in the future under
    both the old and the new setting, so a cycle already noticed keeps its debit date.
    """
    debit_day, notice_lead_days = int(debit_day), int(notice_lead_days)
    if not 1 <= debit_day <= 28:
        raise SettingsError(_("Choose a debit day from 1 to 28."))
    least = notice_days(association, as_of)
    if notice_lead_days < least:
        raise SettingsError(
            _("The notice must go out at least %(n)d days before the debit.") % {"n": least}
        )
    if notice_lead_days > MAX_LEAD_DAYS:
        raise SettingsError(
            _("The notice may go out at most %(n)d days before the debit.") % {"n": MAX_LEAD_DAYS}
        )
    month = month_start(as_of)
    while True:
        old_notice, _old_debit = cycle_dates(association, month)
        new_notice = month.replace(day=debit_day) - dt.timedelta(days=notice_lead_days)
        if old_notice > as_of and new_notice > as_of:
            break
        month = next_month(month)
    from homeoa.core.models import Association

    with transaction.atomic():
        locked = Association.objects.select_for_update().get(pk=association.pk)
        settings = dict(locked.settings or {})
        block = dict(settings.get(SETTINGS_KEY) or {})
        history = [e for e in block.get("history") or [] if e["from"] < month.isoformat()]
        history.append(
            {
                "from": month.isoformat(),
                "debit_day": debit_day,
                "notice_lead_days": notice_lead_days,
                "set_on": as_of.isoformat(),
            }
        )
        block["history"] = history
        settings[SETTINGS_KEY] = block
        locked.settings = settings
        locked.save(update_fields=["settings"])
        association.settings = settings
        audit.record(
            "payments.autopay_settings_changed",
            locked,
            association=locked,
            actor=user if getattr(user, "pk", None) else None,
            changes={
                "debit_day": debit_day,
                "notice_lead_days": notice_lead_days,
                "from": month.isoformat(),
            },
        )
    return month


# ------------------------------------------------------------------ the job


def amount_owed(unit, period, as_of: dt.date, provider=None) -> Decimal:
    """The period's balance less payments still processing (never below zero)."""
    from homeoa.ledger.services import unit_balance

    provider = provider or provider_for(unit.association)
    balance = unit_balance(
        unit.pk, as_of=as_of, ownership_period_id=period.pk, association=unit.association
    )
    pending = sum((p.amount for p in provider.pending(unit, period)), ZERO)
    return max(balance - pending, ZERO)


def _scheduled_charges(enrollment, month: dt.date, as_of: dt.date, debit_on: dt.date) -> Decimal:
    """Charges the period will have by ``debit_on`` that are not in its balance today.

    Posted charges dated after ``as_of``, the month's dues from each active schedule not yet
    posted, and special-assessment installments not yet posted.
    """
    from homeoa.billing.models import AssessmentSchedule, Charge, ChargeKind, SpecialInstallment
    from homeoa.billing.services import BillingError, special, unit_dues

    unit = enrollment.unit
    base = Charge.all_associations.filter(
        association_id=enrollment.association_id,
        unit_id=unit.pk,
        ownership_period_id=enrollment.ownership_period_id,
        void_entry__isnull=True,
    )
    later = base.filter(charge_date__gt=as_of, charge_date__lte=debit_on)
    total = sum((c.amount for c in later), ZERO)
    schedules = AssessmentSchedule.all_associations.filter(
        association_id=enrollment.association_id, active=True
    )
    for schedule in schedules:
        if not schedule.covers(month):
            continue
        if base.filter(kind=ChargeKind.ASSESSMENT, period=month, run__schedule=schedule).exists():
            continue
        total += unit_dues(schedule, [unit]).get(unit.pk, ZERO)
    installments = SpecialInstallment.all_associations.filter(
        special__association_id=enrollment.association_id,
        posted_at__isnull=True,
        charge_date__lte=debit_on,
    ).select_related("special")
    for inst in installments:
        try:
            shares = {u.pk: a for u, a in special.unit_amounts(inst.special, inst.amount)}
        except BillingError:  # bad interests: the projection skips it, never blocks
            continue
        total += shares.get(unit.pk, ZERO)
    return total


def projected_amount(
    enrollment, month: dt.date, as_of: dt.date, provider=None, debit_on: dt.date | None = None
) -> Decimal:
    """The balance owed today plus what the period will be charged by the debit date."""
    from homeoa.ledger.services import unit_balance

    if debit_on is None:
        _notice_on, debit_on = cycle_dates(enrollment.association, month)
    unit, period = enrollment.unit, enrollment.ownership_period
    provider = provider or provider_for(unit.association)
    # Unclamped: a credit today reduces what the coming charges will leave owing.
    balance = unit_balance(
        unit.pk, as_of=as_of, ownership_period_id=period.pk, association=unit.association
    )
    balance -= sum((p.amount for p in provider.pending(unit, period)), ZERO)
    return max(balance + _scheduled_charges(enrollment, month, as_of, debit_on), ZERO)


def statement_shows_debit_date(statement, debit_on: dt.date) -> bool:
    """Whether a sent statement prints this autopay debit.

    Statements store the debit date and amount they print; only a balance-rule line for
    ``debit_on`` can serve as the pre-debit notice (fixed-amount lines are informational).
    """
    return (
        statement.autopay_debit_on == debit_on
        and statement.autopay_amount is not None
        and not statement.autopay_fixed
    )


def _serving_statement(enrollment, debit_on: dt.date, least: int):
    """A sent statement early enough to be the notice, or None."""
    from homeoa.statements.statement_models import Statement

    statement = (
        Statement.objects.filter(
            association_id=enrollment.association_id,
            ownership_period_id=enrollment.ownership_period_id,
            status=Statement.Status.SENT,
            sent_at__isnull=False,
            period_end__lt=debit_on,
        )
        .order_by("-period_end", "-pk")
        .first()
    )
    if statement is None:
        return None
    sent_on = timezone.localtime(statement.sent_at, _tz(enrollment.association)).date()
    if (debit_on - sent_on).days < least:
        return None
    if not statement_shows_debit_date(statement, debit_on):
        return None
    return statement


def _tz(association):
    import zoneinfo

    try:
        return zoneinfo.ZoneInfo(association.timezone)
    except Exception:  # noqa: BLE001
        return timezone.get_current_timezone()


def _notice(enrollment, month, as_of, debit_on, least, provider) -> AutopayCharge:
    """Record (and send, when needed) the notice for this cycle."""
    fixed = enrollment.fixed_amount if enrollment.rule == AutopayRule.FIXED else None
    fields = {
        "association": enrollment.association,
        "enrollment": enrollment,
        "unit": enrollment.unit,
        "period": month,
        "status": AutopayStatus.SCHEDULED,
        "noticed_on": as_of,
        "debit_on": debit_on,
    }
    if fixed is not None:
        # The owner elected the range $0.01..fixed at enrollment, so a fixed debit never
        # needs a notice; the debit is min(fixed, owed) (range election).
        charge = AutopayCharge.all_associations.create(
            **fields, amount=fixed, noticed_amount=fixed, notice_source=NoticeSource.NONE
        )
        audit.record("payments.autopay_scheduled", charge, association=enrollment.association)
        return charge
    statement = _serving_statement(enrollment, debit_on, least)
    if statement is not None:
        amount = max(statement.autopay_amount, ZERO)
    else:
        amount = projected_amount(enrollment, month, as_of, provider)
    charge = AutopayCharge.all_associations.create(
        **fields,
        amount=amount,
        noticed_amount=amount,
        notice_source=NoticeSource.STATEMENT if statement is not None else NoticeSource.NOTICE,
        notice_statement=statement,
    )
    if statement is None and amount > 0 and not _try_notice(charge, as_of):
        # The notice is a precondition of the debit: record the cycle, never
        # debit it, and tell the board (after commit).
        return _skip_notice_failed(charge, as_of)
    audit.record("payments.autopay_noticed", charge, association=enrollment.association)
    return charge


def _skip_notice_failed(charge: AutopayCharge, as_of: dt.date) -> AutopayCharge:
    charge.status = AutopayStatus.SKIPPED_NOTICE_FAILED
    charge.save(update_fields=["status", "message"])
    audit.record(
        "payments.autopay_notice_failed",
        charge,
        association=charge.association,
        message=charge.message,
    )
    transaction.on_commit(lambda: _send_notice_failed(charge, as_of))
    return charge


def _notice_period(charge: AutopayCharge) -> str:
    """Notification period for this cycle's notice and alert; a resend gets its own, so it
    is a new notification rather than the deduplicated first one."""
    month = charge.period.strftime("%Y-%m")
    return f"{month}-resend{charge.notice_resends}" if charge.notice_resends else month


class ResendError(ValueError):
    pass


def resend_notice(charge: AutopayCharge, *, as_of: dt.date, user=None, provider=None):
    """Resend a failed pre-debit notice and reschedule the debit.

    The new debit date is the cycle's debit date or ``notice_days`` after the resend,
    whichever is later; the amount is projected again for that date. If the resend fails
    too, the cycle stays ``skipped_notice_failed`` and the board gets a new alert."""
    from homeoa.core import residents_enabled

    association = charge.association
    with transaction.atomic():
        charge = (
            AutopayCharge.all_associations.select_for_update()
            .select_related("enrollment__ownership_period", "unit")
            .get(pk=charge.pk)
        )
        if charge.status != AutopayStatus.SKIPPED_NOTICE_FAILED:
            raise ResendError(_("Only a cycle whose notice failed can be resent."))
        enrollment = charge.enrollment
        if not enrollment.active:
            raise ResendError(_("Autopay is off for this unit. Collect this month manually."))
        if not residents_enabled(association):
            raise ResendError(_("Owners are not being contacted yet, so no notice can go out."))
        provider = provider or provider_for(association)
        least = notice_days(association, as_of)
        debit_on = max(charge.debit_on or as_of, as_of + dt.timedelta(days=least))
        amount = projected_amount(enrollment, charge.period, as_of, provider, debit_on=debit_on)
        charge.status = AutopayStatus.SCHEDULED
        charge.notice_resends += 1
        charge.noticed_on = as_of
        charge.debit_on = debit_on
        charge.amount = amount
        charge.noticed_amount = amount
        charge.notice_source = NoticeSource.NOTICE
        charge.notice_statement = None
        charge.message = ""
        charge.acknowledged_at = None
        charge.save()
        audit.record(
            "payments.autopay_notice_resent",
            charge,
            association=association,
            actor=user if getattr(user, "pk", None) else None,
            changes={"debit_on": debit_on.isoformat(), "amount": str(amount)},
        )
        if amount > 0 and not _try_notice(charge, as_of):
            return _skip_notice_failed(charge, as_of)
    return charge


def _try_notice(charge: AutopayCharge, as_of: dt.date) -> bool:
    """Send the pre-debit notice; False (with ``charge.message`` set) when it failed.

    It failed when sending raised, reached nobody, or every delivery failed. The send
    runs in a savepoint so its error never undoes the charge record."""
    from homeoa.notify.models import Delivery

    try:
        with transaction.atomic():
            notes = _send_notice(charge, as_of)
    except Exception as exc:
        logger.exception("Pre-debit notice for autopay charge %s failed", charge.pk)
        charge.message = str(exc)[:500]
        return False
    if not notes:
        charge.message = _("No owner could be sent the notice.")
        return False
    return not _deliveries_failed(charge, Delivery.all_associations.filter(notification__in=notes))


def _deliveries_failed(charge: AutopayCharge, deliveries) -> bool:
    """True (and ``charge.message`` set) when every delivery of the notice failed."""
    from homeoa.notify.models import Delivery

    if deliveries.exists() and not deliveries.exclude(status=Delivery.Status.FAILED).exists():
        failed = deliveries.exclude(error="").first()
        charge.message = (failed.error if failed else _("Every delivery failed."))[:500]
        return True
    return False


def _notice_failed_later(charge: AutopayCharge) -> bool:
    """A notice queued for a deferred channel may fail after it was recorded: check again
    before the debit."""
    from homeoa.notify.models import Delivery
    from homeoa.notify.services import object_key_for

    if charge.notice_source != NoticeSource.NOTICE or not charge.noticed_amount:
        return False
    deliveries = Delivery.all_associations.filter(
        notification__association=charge.association,
        notification__type_key=UPCOMING_TYPE,
        notification__object_key=object_key_for(charge),
        notification__period=_notice_period(charge),
    )
    return _deliveries_failed(charge, deliveries)


def _send_notice_failed(charge: AutopayCharge, as_of: dt.date) -> None:
    """Board alert for a cycle skipped because its notice failed (never raises)."""
    from homeoa.notify.services import board_recipients, notify

    association = charge.association
    try:
        words = {"unit": charge.unit.number, "month": charge.period.strftime("%B %Y")}
        subject = (
            _("Pre-debit notice failed for unit %(unit)s; autopay for %(month)s not debited")
            % words
        )
        body = (
            _(
                "The autopay notice for unit %(unit)s (%(month)s) could not be sent, so the "
                "debit will not be made. The owner was not at fault: waive any late fee for "
                "this month. Resend the notice and reschedule, or collect manually."
            )
            % words
        )
        if charge.message:
            body = f"{body}\n\n{charge.message}"
        with transaction.atomic():
            notify(
                NOTICE_FAILED_TYPE,
                board_recipients(association),
                {"subject": subject, "body": body},
                as_of,
                obj=charge,
                period=_notice_period(charge),
                association=association,
                unit=charge.unit,
            )
    except Exception as exc:
        logger.exception("Notice-failed alert for autopay charge %s failed", charge.pk)
        try:
            audit.record(
                "payments.autopay_notify_failed",
                charge,
                association=association,
                message=f"{NOTICE_FAILED_TYPE}: {exc}"[:500],
            )
        except Exception:
            logger.exception("Could not audit the failed alert for charge %s", charge.pk)


def _send_notice(charge: AutopayCharge, as_of: dt.date) -> list:
    from django.utils.formats import date_format

    from homeoa.notify.services import notify

    words = {
        "amount": f"${charge.noticed_amount:,.2f}",
        "unit": charge.unit.number,
        "date": date_format(charge.debit_on, "F j, Y"),
    }
    subject = _("Autopay of up to %(amount)s for unit %(unit)s on %(date)s") % words
    body = (
        _(
            "On %(date)s, autopay will debit up to %(amount)s from your bank account for unit "
            "%(unit)s: your balance today plus the charges due by then. If you pay part of it "
            "first, autopay takes only what is still owed, and never more than this amount. "
            "To change or turn off autopay, use the Pay tab before then."
        )
        % words
    )
    return notify(
        UPCOMING_TYPE,
        _owners(charge.enrollment.ownership_period),
        {"subject": subject, "body": body, "debit_on": charge.debit_on, "amount": words["amount"]},
        as_of,
        obj=charge,
        period=_notice_period(charge),
        association=charge.association,
        unit=charge.unit,
    )


def _idempotency_key(unit, month: dt.date) -> str:
    key = f"autopay-{unit.pk}-{month:%Y-%m}"
    tries = AutopayCharge.all_associations.filter(
        unit=unit, period=month, status=AutopayStatus.FAILED
    ).count()
    return f"{key}-retry{tries}" if tries else key


def _collect(enrollment, charge: AutopayCharge, as_of, provider) -> AutopayCharge:
    """Debit min(noticed amount, balance now); never more than was noticed."""
    from homeoa.providers.payments import PaymentRequest, PaymentStatus

    if _notice_failed_later(charge):
        return _skip_notice_failed(charge, as_of)
    unit, period = enrollment.unit, enrollment.ownership_period
    owed = amount_owed(unit, period, as_of, provider)
    amount = min(charge.noticed_amount or ZERO, owed)
    if enrollment.rule == AutopayRule.FIXED:
        amount = min(amount, enrollment.fixed_amount)  # never more than owed
    charge.enrollment = enrollment
    if amount <= 0:
        charge.amount = ZERO
        charge.status = AutopayStatus.SKIPPED
        charge.save(update_fields=["enrollment", "amount", "status"])
        return charge
    result = provider.collect(
        PaymentRequest(
            association_id=enrollment.association_id,
            unit_id=unit.pk,
            amount=amount,
            description=_("Autopay · Unit %(unit)s · %(month)s")
            % {"unit": unit.number, "month": charge.period.strftime("%Y-%m")},
            idempotency_key=_idempotency_key(unit, charge.period),
            mandate_ref=enrollment.mandate_ref,
            ownership_period_id=period.pk,
        )
    )
    failed = result.status not in (PaymentStatus.PENDING, PaymentStatus.SUCCEEDED)
    charge.amount = amount
    charge.status = AutopayStatus.FAILED if failed else AutopayStatus.PENDING
    charge.reference = result.reference[:100]
    charge.message = str(result.message)[:500]
    charge.failed_at = timezone.now() if failed else None
    charge.save(
        update_fields=["enrollment", "amount", "status", "reference", "message", "failed_at"]
    )
    if failed:
        _notify_failed(charge, as_of)
    return charge


def run(association, as_of: dt.date) -> AutopayRun | None:
    """Send due pre-debit notices and start due debits (idempotent per unit and month).

    For each active enrollment and each cycle (month) from this month on: from its notice
    date, while at least ``autopay.notice_days`` remain before the debit, the cycle is
    noticed (``scheduled``); on or after the noticed debit date it is debited. A cycle with
    no notice is never debited.
    """
    from homeoa.core import residents_enabled

    if not residents_enabled(association):
        return None
    provider = provider_for(association)
    if not getattr(provider, "online", False):
        return None
    least = notice_days(association, as_of)
    enrollments = (
        AutopayEnrollment.all_associations.filter(
            association=association, cancelled_at__isnull=True
        )
        .select_related("unit", "ownership_period", "association")
        .order_by("unit__number")
    )
    started = []
    for enrollment in enrollments:
        period = enrollment.ownership_period
        if period.end is not None and period.end < as_of:
            cancel(enrollment)  # the owner sold the unit
            continue
        month = month_start(as_of)
        # A resent notice may move a cycle's debit past its own month.
        late = AutopayCharge.all_associations.filter(
            unit=enrollment.unit, status=AutopayStatus.SCHEDULED, period__lt=month
        ).values_list("period", flat=True)
        for period_month in sorted(set(late)):
            charge = _step(enrollment, period_month, as_of, least, provider)
            if charge is not None and charge.status == AutopayStatus.PENDING:
                started.append(charge)
        for _i in range(MONTHS_AHEAD):
            charge = _step(enrollment, month, as_of, least, provider)
            if charge is not None and charge.status == AutopayStatus.PENDING:
                started.append(charge)
            month = next_month(month)
    if not started:
        return None
    batch = AutopayRun.all_associations.create(
        association=association,
        as_of=as_of,
        units=len(started),
        total=sum((c.amount for c in started), ZERO),
    )
    audit.record("payments.autopay_collected", batch, association=association)
    return batch


def _step(enrollment, month, as_of, least, provider) -> AutopayCharge | None:
    if enrollment.starts_on > month:
        return None
    # A failed month is never retried, so any charge for the month settles it.
    charge = AutopayCharge.all_associations.filter(unit=enrollment.unit, period=month).first()
    with transaction.atomic():
        if charge is None:
            notice_on, debit_on = cycle_dates(enrollment.association, month)
            if as_of < notice_on or (debit_on - as_of).days < least:
                return None  # not yet, or too late to give notice: no debit this cycle
            return _notice(enrollment, month, as_of, debit_on, least, provider)
        if charge.status == AutopayStatus.SCHEDULED and as_of >= charge.debit_on:
            charge = AutopayCharge.all_associations.select_for_update().get(pk=charge.pk)
            if charge.status == AutopayStatus.SCHEDULED:
                return _collect(enrollment, charge, as_of, provider)
    return None


# ------------------------------------------------------------------ failures


def reference_failed(association, reference: str, message: str = "") -> AutopayCharge | None:
    """The provider reports that the debit ``reference`` failed (called from webhooks)."""
    if not reference:
        return None
    charge = (
        AutopayCharge.all_associations.filter(association=association, reference=reference)
        .select_related("unit", "enrollment__ownership_period")
        .first()
    )
    if charge is None or charge.status == AutopayStatus.FAILED:
        return charge
    charge.status = AutopayStatus.FAILED
    charge.message = (message or "")[:500]
    charge.failed_at = timezone.now()
    charge.save(update_fields=["status", "message", "failed_at"])
    audit.record("payments.autopay_failed", charge, association=association)
    from homeoa.core.context import association_today

    _notify_failed(charge, association_today(association))
    return charge


def _notify_failed(charge: AutopayCharge, as_of: dt.date) -> None:
    """Alert the owner and the board after the failure commits.

    A notification never rolls back or blocks the state change: each send runs on commit
    and any error is logged and audited, never raised."""
    transaction.on_commit(lambda: _send_failed(charge, as_of, OWNER_TYPE))
    transaction.on_commit(lambda: _send_failed(charge, as_of, BOARD_TYPE))


def _send_failed(charge: AutopayCharge, as_of: dt.date, type_key: str) -> None:
    from homeoa.notify.services import board_recipients, notify

    association = charge.association
    try:
        words = {
            "amount": f"${charge.amount:,.2f}",
            "unit": charge.unit.number,
            "month": charge.period.strftime("%B %Y"),
        }
        subject = _("Autopay of %(amount)s for unit %(unit)s failed") % words
        reason = charge.message or _("The bank did not accept the debit.")
        if type_key == OWNER_TYPE:
            owner_body = (
                _(
                    "Your autopay of %(amount)s for unit %(unit)s (%(month)s) did not go "
                    "through. Please pay this month's balance from the Pay tab or contact "
                    "the board."
                )
                % words
            )
            recipients = _owners(charge.enrollment.ownership_period)
            content = {"subject": subject, "body": f"{owner_body}\n\n{reason}"}
        else:
            recipients = board_recipients(association)
            content = {"subject": subject, "body": str(reason)}
        with transaction.atomic():
            notify(
                type_key,
                recipients,
                content,
                as_of,
                obj=charge,
                period=charge.period.strftime("%Y-%m"),
                association=association,
                unit=charge.unit,
            )
    except Exception as exc:
        logger.exception("Autopay failure notice %s for charge %s failed", type_key, charge.pk)
        try:
            audit.record(
                "payments.autopay_notify_failed",
                charge,
                association=association,
                message=f"{type_key}: {exc}"[:500],
            )
        except Exception:
            logger.exception("Could not audit the failed notice for charge %s", charge.pk)


NEEDS_FOLLOW_UP = (AutopayStatus.FAILED, AutopayStatus.SKIPPED_NOTICE_FAILED)


def acknowledge(charge: AutopayCharge, *, user=None) -> AutopayCharge:
    """The board followed up a failed debit or a skipped (notice failed) cycle."""
    if charge.status in NEEDS_FOLLOW_UP and charge.acknowledged_at is None:
        charge.acknowledged_at = timezone.now()
        charge.save(update_fields=["acknowledged_at"])
        audit.record(
            "payments.autopay_failure_acknowledged",
            charge,
            association=charge.association,
            actor=user if getattr(user, "pk", None) else None,
        )
    return charge


def todo_items(association) -> list[dict]:
    """Board to-dos: failed debits and notice-failed (skipped) cycles not yet followed up."""
    from django.urls import reverse

    from homeoa.core.context import association_today

    items = []
    failed = AutopayCharge.all_associations.filter(
        association=association, status=AutopayStatus.FAILED, acknowledged_at__isnull=True
    ).select_related("unit")
    for charge in failed:
        items.append(_failed_todo(charge, association_today(association)))
    skipped = AutopayCharge.all_associations.filter(
        association=association,
        status=AutopayStatus.SKIPPED_NOTICE_FAILED,
        acknowledged_at__isnull=True,
    ).select_related("unit")
    for charge in skipped:
        items.append(
            {
                "id": f"autopay-notice-failed-{charge.pk}",
                "kind": "money",
                "title": _(
                    "Pre-debit notice failed for unit %(unit)s; resend notice and "
                    "reschedule, or collect manually"
                )
                % {"unit": charge.unit.number},
                "sub": _(
                    "Autopay for %(month)s was not debited. The owner was not at fault: "
                    "waive any late fee for this month."
                )
                % {"month": charge.period.strftime("%B %Y")},
                "due": association_today(association),
                "url": reverse("payments:autopay_settings") + f"#autopay-charge-{charge.pk}",
                "action": _("Resend notice"),
                "post_url": reverse("payments:autopay_resend", args=[charge.pk]),
                "post_data": {},
            }
        )
    return items


def _failed_todo(charge: AutopayCharge, today: dt.date) -> dict:
    from django.urls import reverse

    return {
        "id": f"autopay-failed-{charge.pk}",
        "kind": "money",
        "title": _("Autopay of %(amount)s from unit %(unit)s failed")
        % {"amount": f"${charge.amount:,.2f}", "unit": charge.unit.number},
        "sub": charge.message or _("The bank did not accept the debit."),
        "due": today,
        "url": reverse("registry:unit", args=[charge.unit.number]),
        "action": _("Got it"),
        "post_url": reverse("payments:autopay_ack", args=[charge.pk]),
        "post_data": {},
    }


def history(association, *, unit=None, ownership_period=None, limit: int = 24):
    """Autopay cycles, newest first, for the board (all or one unit) or an owner."""
    qs = AutopayCharge.all_associations.filter(association=association).select_related("unit")
    if unit is not None:
        qs = qs.filter(unit=unit)
    if ownership_period is not None:
        qs = qs.filter(enrollment__ownership_period=ownership_period)
    return list(qs.order_by("-period", "-pk")[:limit])


def statement_line(period, as_of: dt.date) -> dict:
    """The next autopay debit to print on the statement.

    The debit is the first cycle not yet due for its notice on ``as_of`` (the later of the
    statement's end and the day it is generated), so a statement sent promptly can serve as
    that cycle's notice. Balance-rule enrollments get the projected amount (balance as
    of ``as_of`` plus charges due by the debit date, an upper bound); fixed-amount
    enrollments get the fixed amount (informational: never a notice). Empty when the
    period has no active enrollment or ends before the debit.
    """
    enrollment = active_enrollment(period)
    if enrollment is None:
        return {}
    debit_month, _notice_on, debit_on = next_cycle(period.association, as_of)
    if period.end is not None and period.end < debit_on:
        return {}
    if enrollment.rule == AutopayRule.FIXED:
        return {
            "autopay_debit_on": debit_on,
            "autopay_amount": enrollment.fixed_amount,
            "autopay_fixed": True,
        }
    amount = projected_amount(enrollment, debit_month, as_of)
    return {"autopay_debit_on": debit_on, "autopay_amount": amount, "autopay_fixed": False}
