"""Delinquency automation (FIN-17): payment reminder, late notice, demand letter.

Offsets
-------
Each step's offset is a rule-pack value, ``delinquency.<step>_days`` (days after the due
date of the unit's oldest unpaid charge), read through ``rules.delinquency_offsets``. The
board overrides them at ``/settings/delinquency/`` (``rules.set_override``: reason
required, audited). No values live in core: the generic pack leaves them unset, and an
unset step is simply skipped. Every value is shown with its verified/unverified badge.

Interest and late fees are not computed here; they are billing's late-fee policy (FIN-09),
whose interest rate also comes from the rule pack.

Daily job (``run(association, as_of)``)
---------------------------------------
1. A unit is delinquent when it has a non-void charge with an open balance whose due date
   is before ``as_of``. It gets one open ``DelinquencyCase`` anchored on the oldest such
   due date (unit G in the demo: Sep 10).
2. Units with an open case and nothing past due (paid in full) have the case closed; the
   ladder stops.
3. A case that is not on hold advances at most one step per run: the next step that is
   configured, not yet taken or skipped, and whose ``anchor + offset <= as_of``.
4. The step generates its letter (template ``reminder`` / ``late-notice`` /
   ``demand``). Reminder and late notice are routed per owner: owners whose channel is
   email (e-delivery consent) get a ``notify`` message filed on the unit; the
   others get the letter in a draft postal batch. While residents are off,
   notify stores ``preview`` deliveries (``preview_only=True``, so board-member
   owners and residents-on included) and batches stay drafts, so nothing is sent.
5. The demand step only generates a draft letter and raises a board to-do; it is never
   routed or sent automatically.

The board may put a case on hold (no further steps) or skip its next step; both audited.

Statutory notices (e.g. WA RCW 64.34.364(17)-(18))
---------------------------------------------------------
The pack decides: when ``delinquency.notice.statutory_text`` and
``delinquency.first_notice.max_days_past_due`` are both set, the late-notice step is
replaced by the statutory first and second notices (``statutory_mode``):

* First notice: generated at ``late_notice_days`` (board, capped at the deadline) or, when
  unset, on the first run after the due date; due by ``anchor + max_days_past_due``. A
  board to-do shows the deadline and turns overdue past it while the notice is not mailed.
* Every notice is a letter per address (the unit address and every other address the
  owners gave) in one draft postal batch (``notice.mail_required``), plus an email copy to
  each owner with a known address whatever their e-delivery consent
  (``notice.email_copy_if_known``; statute, bypasses e-notice consent). Previews only
  while residents are off, like every other step.
* The body ends with the statutory block in force on the notice date (the pack's
  effective-dated value picks e.g. the 2028 RCW 64.90.485 variant), printed verbatim and
  locked (``homeoa.letters.statutory``). Missing Commerce/HUD/legal-aid contacts print a
  placeholder, mark the notice and raise a board to-do.
* Standstill: no other step until ``standstill_days`` after the first notice was mailed.
  "Mailed" is the batch's send/confirm date; until then the notice date stands in, so the
  preview timeline still moves (nothing is sent while previews are drafts anyway).
* Second notice: from ``max(anchor + second_notice.min_days_past_due,
  mailed + second_notice.min_days_after_first)``, same content and channels.
* Foreclosure threshold: owed assessments (assessment and special-assessment charges
  only; fines, late fees, interest and other charges excluded) reaching
  ``max(foreclosure.min_months_assessments x monthly assessment, foreclosure.min_amount)``
  are dated on the case; once ``foreclosure.min_days_after_threshold`` days have passed
  and the second notice exists, a board to-do says foreclosure needs a board vote and an
  attorney. HomeOA never starts collection or foreclosure actions itself.
* Each copy is a ``PreforeclosureRecord`` (protected; listed on the case).

Preferred language
--------------------------------
When any current owner's ``preferred_language`` is not English, a statutory notice is held
("waiting for translation") until the board uploads an approved translation of the notice
template for that language (``letters.LetterTranslation``). The hold raises a board to-do
that shows the unchanged first-notice deadline. Once uploaded, the next run sends the
notice in English followed by the translation: a text translation is printed after the
statutory block (and added to the email copy), a PDF translation is appended to the letter
PDF (and attached, as the same PDF, to the email copy). HomeOA never
machine-translates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Min, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, rules
from homeoa.core import status
from homeoa.notify import types as ntypes

from .models import DelinquencyAction, DelinquencyCase, PreforeclosureRecord, Step

STEPS: tuple[str, ...] = (Step.REMINDER, Step.LATE_NOTICE, Step.DEMAND)
NOTICE_STEPS: tuple[str, ...] = (Step.FIRST_NOTICE, Step.SECOND_NOTICE)
NOTICE_TEMPLATE_KEY = "wa-delinquency-notice"
NOTICE_TEMPLATE_KIND = "delinquency_notice"
NOTICE_NOTIFICATION = "statements.delinquency_notice"
ASSESSMENT_KINDS = ("assessment", "special")
TEMPLATE_KEYS = {
    Step.REMINDER: "reminder",
    Step.LATE_NOTICE: "late-notice",
    Step.DEMAND: "demand",
}
TEMPLATE_KINDS = {
    Step.REMINDER: "reminder",
    Step.LATE_NOTICE: "late_notice",
    Step.DEMAND: "demand",
}
NOTIFICATION_TYPES = {
    Step.REMINDER: "statements.delinquency_reminder",
    Step.LATE_NOTICE: "statements.delinquency_late_notice",
}
EMAIL = "email"


class DelinquencyError(Exception):
    pass


def register_notification_types() -> None:
    labels = {
        Step.REMINDER: gettext_lazy("Payment reminder"),
        Step.LATE_NOTICE: gettext_lazy("Late notice"),
    }
    ntypes.register(
        ntypes.NotificationType(
            key=NOTICE_NOTIFICATION,
            label=gettext_lazy("Notice of delinquency"),
            default_channels=(EMAIL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            mandatory=True,
            subject_template="{{ subject }}",
            body_template="{% autoescape off %}{{ body }}{% endautoescape %}",
        )
    )
    for step, key in NOTIFICATION_TYPES.items():
        ntypes.register(
            ntypes.NotificationType(
                key=key,
                label=labels[step],
                default_channels=(EMAIL,),
                audience=ntypes.AUDIENCE_RESIDENTS,
                mandatory=True,
                subject_template="{{ subject }}",
                body_template="{% autoescape off %}{{ body }}{% endautoescape %}",
            )
        )


# --- reading ----------------------------------------------------------------------------


def offsets(association) -> dict[str, int | None]:
    return {step: r.value for step, r in rules.delinquency_offsets(association).items()}


def _overdue(association, as_of: dt.date):
    from homeoa.billing.models import Charge

    return Charge.all_associations.filter(
        association=association,
        void_entry__isnull=True,
        open_balance__gt=0,
        due_date__lt=as_of,
    )


def overdue_by_unit(association, as_of: dt.date) -> dict[int, dict]:
    """``{unit_id: {"oldest": date, "balance": Decimal}}`` for units with past-due charges."""
    rows = (
        _overdue(association, as_of)
        .values("unit_id")
        .annotate(oldest=Min("due_date"), balance=Sum("open_balance"))
    )
    return {r["unit_id"]: {"oldest": r["oldest"], "balance": r["balance"]} for r in rows}


def oldest_due(case: DelinquencyCase, as_of: dt.date) -> dt.date:
    """The real oldest past-due date for the case's unit. The case's anchor can be later
    (the go-live date, for arrears loaded from before go-live)."""
    info = overdue_by_unit(case.association, as_of).get(case.unit_id)
    return min(case.anchor_due_date, info["oldest"]) if info else case.anchor_due_date


def taken_steps(case: DelinquencyCase) -> set[str]:
    return set(case.actions.values_list("step", flat=True))


def next_step(case: DelinquencyCase, configured: dict[str, int | None]):
    """``(step, due_on)`` of the next configured step not yet taken, or ``None``."""
    taken = taken_steps(case)
    for step in STEPS:
        days = configured.get(step)
        if step in taken or days is None:
            continue
        return step, case.anchor_due_date + dt.timedelta(days=days)
    return None


# --- the job ----------------------------------------------------------------------------


@dataclass
class RunResult:
    opened: list[DelinquencyCase] = field(default_factory=list)
    closed: list[DelinquencyCase] = field(default_factory=list)
    actions: list[DelinquencyAction] = field(default_factory=list)
    held: list[DelinquencyCase] = field(default_factory=list)


def _close(case: DelinquencyCase, as_of: dt.date, reason: str) -> None:
    case.status = DelinquencyCase.Status.CLOSED
    case.closed_on = as_of
    case.closed_reason = reason
    case.save(update_fields=["status", "closed_on", "closed_reason"])
    audit.record(
        "delinquency.close",
        case,
        changes={"unit": case.unit.number, "as_of": as_of.isoformat(), "reason": reason},
    )


def run(association, as_of: dt.date, *, user=None) -> RunResult:
    """Advance every delinquent unit by the configured offsets (idempotent per day)."""
    from homeoa.letters.services import ensure_default_templates

    result = RunResult()
    ensure_default_templates(association)
    overdue = overdue_by_unit(association, as_of)
    configured = offsets(association)
    go_live = status.go_live_date(association)
    open_cases = {
        c.unit_id: c
        for c in DelinquencyCase.all_associations.filter(
            association=association, status=DelinquencyCase.Status.OPEN
        ).select_related("unit")
    }
    for unit_id, case in open_cases.items():
        if unit_id not in overdue:
            _close(case, as_of, _("Paid in full"))
            result.closed.append(case)
    postal: list[tuple[DelinquencyAction, list]] = []
    statutory = statutory_config(association, as_of)
    for unit_id, info in sorted(overdue.items()):
        case = open_cases.get(unit_id)
        if case is None:
            case = DelinquencyCase.all_associations.create(
                association=association,
                unit_id=unit_id,
                # Arrears loaded from before go-live start the ladder fresh at go-live.
                anchor_due_date=max(info["oldest"], go_live) if go_live else info["oldest"],
                opened_on=as_of,
            )
            result.opened.append(case)
        if statutory:
            track_threshold(case, as_of, statutory)
        if case.on_hold:
            continue
        if statutory:
            nxt = statutory_next(case, as_of, configured, statutory)
        else:
            nxt = next_step(case, configured)
        if nxt is None or nxt[1] > as_of:
            continue
        if nxt[0] in NOTICE_STEPS and missing_translations(case, as_of):
            result.held.append(case)
            continue
        if nxt[0] in NOTICE_STEPS:
            action, letters = take_notice(case, nxt[0], as_of, statutory, user=user)
        else:
            action, needs_mail = take_step(case, nxt[0], as_of, user=user)
            letters = [action.letter] if needs_mail else []
        result.actions.append(action)
        if letters:
            postal.append((action, letters))
    if postal:
        _batch(association, postal, as_of, user)
    return result


# --- statutory notices ------------------------------------------------------------------


STATUTORY_KEYS = {
    "text": "delinquency.notice.statutory_text",
    "first_max": "delinquency.first_notice.max_days_past_due",
    "standstill": "delinquency.standstill_days",
    "second_past_due": "delinquency.second_notice.min_days_past_due",
    "second_after_first": "delinquency.second_notice.min_days_after_first",
    "months": "delinquency.foreclosure.min_months_assessments",
    "min_amount": "delinquency.foreclosure.min_amount",
    "after_threshold": "delinquency.foreclosure.min_days_after_threshold",
    "mail": "delinquency.notice.mail_required",
    "email": "delinquency.notice.email_copy_if_known",
}


def statutory_config(association, as_of: dt.date) -> dict | None:
    """The statutory-notice rule values in force on ``as_of``, or ``None`` (pack off)."""
    cfg = {name: rules.value(association, key, as_of) for name, key in STATUTORY_KEYS.items()}
    if not cfg["text"] or cfg["first_max"] is None:
        return None
    return cfg


def _done(case: DelinquencyCase) -> dict[str, DelinquencyAction]:
    return {a.step: a for a in case.actions.select_related("batch")}


def _is_done(action) -> bool:
    return action is not None and action.outcome == DelinquencyAction.Outcome.DONE


def is_mailed(action: DelinquencyAction) -> bool:
    from homeoa.letters.models import MailBatch

    return action.batch is not None and action.batch.status != MailBatch.Status.DRAFT


def mailed_on(action: DelinquencyAction) -> dt.date:
    """When the notice was mailed: its batch's send/confirm date, else the notice date."""
    if is_mailed(action):
        when = action.batch.confirmed_at or action.batch.sent_at
        if when is not None:
            return timezone.localdate(when)
    return action.as_of


def first_notice_deadline(case: DelinquencyCase, cfg: dict) -> dt.date:
    return case.anchor_due_date + dt.timedelta(days=cfg["first_max"])


def standstill_until(case: DelinquencyCase, cfg: dict) -> dt.date | None:
    """Last day of the standstill after the first notice (``None``: no first notice)."""
    first = _done(case).get(Step.FIRST_NOTICE)
    if not _is_done(first):
        return None
    return mailed_on(first) + dt.timedelta(days=(cfg["standstill"] or 0))


def statutory_schedule(case: DelinquencyCase, configured: dict, cfg: dict) -> list:
    """``[(step, earliest_date)]`` of the untaken steps, earliest first."""
    anchor = case.anchor_due_date
    taken = _done(case)
    first = taken.get(Step.FIRST_NOTICE)
    out = []
    if first is None:
        if configured.get(Step.REMINDER) is not None and Step.REMINDER not in taken:
            out.append((Step.REMINDER, anchor + dt.timedelta(days=configured[Step.REMINDER])))
        days = configured.get(Step.LATE_NOTICE)
        # Unset: drafted the first day past due, leaving the board time to mail it.
        days = 1 if days is None else min(max(days, 1), cfg["first_max"])
        out.append((Step.FIRST_NOTICE, anchor + dt.timedelta(days=days)))
        return sorted(out, key=lambda x: x[1])
    until = standstill_until(case, cfg)
    after = until + dt.timedelta(days=1) if until else anchor
    if Step.SECOND_NOTICE not in taken and _is_done(first):
        earliest = max(
            anchor + dt.timedelta(days=cfg["second_past_due"] or 0),
            mailed_on(first) + dt.timedelta(days=cfg["second_after_first"] or 0),
            after,
        )
        out.append((Step.SECOND_NOTICE, earliest))
    if configured.get(Step.DEMAND) is not None and Step.DEMAND not in taken:
        out.append((Step.DEMAND, max(anchor + dt.timedelta(days=configured[Step.DEMAND]), after)))
    return sorted(out, key=lambda x: x[1])


def statutory_next(case, as_of, configured, cfg):
    schedule = statutory_schedule(case, configured, cfg)
    return schedule[0] if schedule else None


def upcoming(case: DelinquencyCase, as_of: dt.date, configured=None):
    """``(step, date)`` of the case's next step in whichever mode is active."""
    configured = configured if configured is not None else offsets(case.association)
    cfg = statutory_config(case.association, as_of)
    if cfg:
        return statutory_next(case, as_of, configured, cfg)
    return next_step(case, configured)


def owed_assessments(case: DelinquencyCase, as_of: dt.date) -> Decimal:
    """Past-due assessments only: fines, late fees, interest and other charges excluded."""
    total = (
        _overdue(case.association, as_of)
        .filter(unit_id=case.unit_id, kind__in=ASSESSMENT_KINDS)
        .aggregate(t=Sum("open_balance"))["t"]
    )
    return total or Decimal("0")


def monthly_assessment(case: DelinquencyCase, as_of: dt.date) -> Decimal:
    """The unit's latest regular assessment charge (its monthly dues)."""
    from homeoa.billing.models import Charge

    last = (
        Charge.all_associations.filter(
            association=case.association,
            unit_id=case.unit_id,
            kind="assessment",
            void_entry__isnull=True,
            charge_date__lte=as_of,
        )
        .order_by("-charge_date", "-pk")
        .first()
    )
    return last.amount if last else Decimal("0")


def foreclosure_threshold(case: DelinquencyCase, as_of: dt.date, cfg: dict) -> Decimal | None:
    if cfg["months"] is None and cfg["min_amount"] is None:
        return None
    by_months = monthly_assessment(case, as_of) * (cfg["months"] or 0)
    return max(by_months, cfg["min_amount"] or Decimal("0"))


def track_threshold(case: DelinquencyCase, as_of: dt.date, cfg: dict) -> None:
    threshold = foreclosure_threshold(case, as_of, cfg)
    reached = bool(threshold) and owed_assessments(case, as_of) >= threshold
    if reached == (case.threshold_reached_on is not None):
        return
    case.threshold_reached_on = as_of if reached else None
    case.save(update_fields=["threshold_reached_on"])
    audit.record(
        "delinquency.threshold",
        case,
        changes={
            "unit": case.unit.number,
            "as_of": as_of.isoformat(),
            "reached": reached,
            "threshold": str(threshold),
        },
    )


def foreclosure_ready(case: DelinquencyCase, as_of: dt.date, cfg: dict) -> bool:
    """The rule-pack conditions a board would check before voting on foreclosure."""
    if case.threshold_reached_on is None:
        return False
    if not _is_done(_done(case).get(Step.SECOND_NOTICE)):
        return False
    wait = dt.timedelta(days=cfg["after_threshold"] or 0)
    return as_of >= case.threshold_reached_on + wait


def notice_addresses(unit, owners) -> list[list[str]]:
    """The unit address and every other address the owners gave, without repeats."""
    out: list[list[str]] = []
    seen: list[list[str]] = []
    for lines in [list(unit.address_lines), *[list(p.address_lines) for p in owners]]:
        key = [x.strip().lower() for x in lines]
        if lines and key not in seen:
            seen.append(key)
            out.append(lines)
    return out


def owner_languages(case: DelinquencyCase, as_of: dt.date) -> list[str]:
    """Non-English preferred languages of the unit's current owners, sorted."""
    from homeoa.registry.models import ENGLISH
    from homeoa.registry.services import current_owners

    langs = {p.preferred_language for p in current_owners(case.unit, as_of)}
    return sorted(x for x in langs if x and x != ENGLISH)


def notice_translations(case: DelinquencyCase, as_of: dt.date) -> dict:
    """``{language: LetterTranslation | None}`` for the owners' non-English languages."""
    from homeoa.letters.services import current_translation

    langs = owner_languages(case, as_of)
    if not langs:
        return {}
    template = _notice_template(case.association)
    return {lang: current_translation(template, lang) for lang in langs}


def missing_translations(case: DelinquencyCase, as_of: dt.date) -> list[str]:
    """Languages the statutory notice is waiting for (the language hold)."""
    return [lang for lang, t in notice_translations(case, as_of).items() if t is None]


def _notice_template(association):
    from homeoa.letters.models import LetterTemplate
    from homeoa.letters.services import ensure_default_templates

    ensure_default_templates(association)
    templates = LetterTemplate.all_associations.filter(
        association=association, current_version__isnull=False
    )
    found = templates.filter(key=NOTICE_TEMPLATE_KEY).first()
    found = found or templates.filter(kind=NOTICE_TEMPLATE_KIND).order_by("pk").first()
    if found is None:
        raise DelinquencyError(_("No letter template for the notice of delinquency."))
    return found


@transaction.atomic
def take_notice(case: DelinquencyCase, step: str, as_of: dt.date, cfg: dict, *, user=None):
    """Generate a statutory notice: a letter per address and the email copies.

    Returns ``(action, letters_to_mail)``.
    """
    from homeoa.letters import render, statutory
    from homeoa.letters.services import context_for, generate_letter
    from homeoa.notify.services import notify_after_commit
    from homeoa.registry.services import current_owners

    association = case.association
    text_id = cfg["text"]
    locked, missing = statutory.render(text_id, association)
    template = _notice_template(association)
    owners = current_owners(case.unit, as_of)
    addresses = notice_addresses(case.unit, owners) or [None]
    from homeoa.letters.services import translation_bytes

    translations = [t for t in notice_translations(case, as_of).values() if t is not None]
    text_parts = [t.text for t in translations if t.text and not t.is_pdf]
    pdfs = [translation_bytes(t) for t in translations if t.is_pdf]
    printed = "\n\n".join([locked, *text_parts])
    letters = [
        generate_letter(
            template,
            case.unit,
            as_of=as_of,
            user=user,
            address_lines=a,
            locked_text=printed,
            appendix_pdfs=pdfs,
        )
        for a in addresses
    ]
    action = DelinquencyAction.all_associations.create(
        association=association,
        case=case,
        step=step,
        as_of=as_of,
        letter=letters[0],
        statutory_text=text_id,
        contacts_missing=missing,
        created_by=user if user is not None and user.is_authenticated else None,
    )
    for letter in letters:
        PreforeclosureRecord.all_associations.create(
            association=association,
            action=action,
            channel=PreforeclosureRecord.Channel.MAIL,
            letter=letter,
            address_lines=letter.address_lines,
        )
    if cfg["email"]:
        version = letters[0].template_version
        _subject, body = render.render_letter(
            version.subject, version.body, context_for(association, case.unit, as_of)
        )
        body = f"{body.rstrip()}\n\n{printed}"
        attachments = [
            {
                "name": t.filename or f"notice-{t.language}.pdf",
                "content_type": "application/pdf",
                "file_key": t.file_key,
            }
            for t in translations
            if t.is_pdf
        ]
        for person in owners:
            if not person.email:
                continue
            record = PreforeclosureRecord.all_associations.create(
                association=association,
                action=action,
                channel=PreforeclosureRecord.Channel.EMAIL,
                letter=letters[0],
                email=person.email,
            )
            # The email copy is created after commit; a failure is logged and
            # audited and never undoes the recorded step.
            notify_after_commit(
                NOTICE_NOTIFICATION,
                [person],
                {"subject": letters[0].subject, "body": body},
                as_of,
                obj=letters[0],
                period=f"delinquency:{case.pk}:{step}",
                association=association,
                unit=case.unit,
                preview_only=True,
                # Statute: also by email whenever the address is known, consent or not.
                force_channels=(EMAIL,),
                attachments=attachments,
                on_sent=_linker(record),
            )
    audit.record(
        "delinquency.step",
        case,
        changes={
            "unit": case.unit.number,
            "step": step,
            "as_of": as_of.isoformat(),
            "letters": [x.pk for x in letters],
            "statutory_text": text_id,
            "contacts_missing": missing,
            "anchor_due_date": case.anchor_due_date.isoformat(),
            "translations": {t.language: t.pk for t in translations},
        },
        actor=user,
    )
    return action, (letters if cfg["mail"] is not False else [])


def _linker(record):
    def link(notes) -> None:
        if notes:
            record.notification = notes[0]
            record.save(update_fields=["notification"])

    return link


def _template(association, step: str):
    from homeoa.letters.models import LetterTemplate

    templates = LetterTemplate.all_associations.filter(
        association=association, current_version__isnull=False
    )
    found = templates.filter(key=TEMPLATE_KEYS[step]).first()
    found = found or templates.filter(kind=TEMPLATE_KINDS[step]).order_by("pk").first()
    if found is None:
        raise DelinquencyError(_("No letter template for %(step)s.") % {"step": Step(step).label})
    return found


@transaction.atomic
def take_step(case: DelinquencyCase, step: str, as_of: dt.date, *, user=None):
    """Generate the step's letter and route it. Returns ``(action, needs_postal)``."""
    from homeoa.letters import render
    from homeoa.letters.services import context_for, generate_letter

    association = case.association
    template = _template(association, step)
    letter = generate_letter(template, case.unit, as_of=as_of, user=user)
    action = DelinquencyAction.all_associations.create(
        association=association,
        case=case,
        step=step,
        as_of=as_of,
        letter=letter,
        created_by=user if user is not None and user.is_authenticated else None,
    )
    needs_mail = False
    if step != Step.DEMAND:
        from homeoa.notify.services import channels_for, notify_after_commit
        from homeoa.registry.services import current_owners

        ntype = ntypes.get(NOTIFICATION_TYPES[step])
        owners = current_owners(case.unit, as_of)
        by_email = [p for p in owners if EMAIL in channels_for(ntype, p)]
        needs_mail = len(by_email) < len(owners)
        if by_email:
            version = letter.template_version
            _subject, body = render.render_letter(
                version.subject, version.body, context_for(association, case.unit, as_of)
            )
            notify_after_commit(  # After commit, never raises
                ntype.key,
                by_email,
                {"subject": letter.subject, "body": body},
                as_of,
                obj=letter,
                period=f"delinquency:{case.pk}:{step}",
                association=association,
                unit=case.unit,
                # Never sent automatically, not even to board-member owners.
                # TODO: a board "release" action that queues reviewed previews for sending.
                preview_only=True,
            )
    audit.record(
        "delinquency.step",
        case,
        changes={
            "unit": case.unit.number,
            "step": step,
            "as_of": as_of.isoformat(),
            "letter": letter.pk,
            "anchor_due_date": case.anchor_due_date.isoformat(),
        },
        actor=user,
    )
    return action, needs_mail


def _batch(association, postal: list, as_of: dt.date, user) -> None:
    from homeoa.letters.batches import create_batch

    batch = create_batch(
        association,
        [letter for _action, letters in postal for letter in letters],
        name=_("Delinquency letters %(date)s") % {"date": as_of.isoformat()},
        user=user,
    )
    pks = [a.pk for a, _letters in postal]
    DelinquencyAction.all_associations.filter(pk__in=pks).update(batch=batch)
    for a, _letters in postal:
        a.batch = batch


# --- board actions ----------------------------------------------------------------------


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise DelinquencyError(_("A reason is required."))
    return reason


@transaction.atomic
def set_hold(case: DelinquencyCase, on_hold: bool, *, reason: str, user=None) -> None:
    reason = _reason(reason)
    old = case.on_hold
    case.on_hold = on_hold
    case.hold_reason = reason if on_hold else ""
    case.save(update_fields=["on_hold", "hold_reason"])
    audit.record(
        "delinquency.hold" if on_hold else "delinquency.release",
        case,
        changes={"unit": case.unit.number, "old": old, "new": on_hold, "reason": reason},
        message=reason,
        actor=user,
    )


@transaction.atomic
def skip_next(case: DelinquencyCase, as_of: dt.date, *, reason: str, user=None):
    """Skip the case's next configured step (it will not be taken)."""
    reason = _reason(reason)
    nxt = upcoming(case, as_of)
    if nxt is None:
        raise DelinquencyError(_("There is no next step to skip."))
    action = DelinquencyAction.all_associations.create(
        association=case.association,
        case=case,
        step=nxt[0],
        outcome=DelinquencyAction.Outcome.SKIPPED,
        as_of=as_of,
        reason=reason,
        created_by=user if user is not None and user.is_authenticated else None,
    )
    audit.record(
        "delinquency.skip",
        case,
        changes={"unit": case.unit.number, "step": nxt[0], "reason": reason},
        message=reason,
        actor=user,
    )
    return action


def mark_reviewed(action: DelinquencyAction, *, user=None) -> None:
    if action.reviewed_at is not None:
        return
    action.reviewed_at = timezone.now()
    action.save(update_fields=["reviewed_at"])
    audit.record(
        "delinquency.demand_reviewed",
        action.case,
        changes={"unit": action.case.unit.number, "letter": action.letter_id},
        actor=user,
    )


@dataclass
class EmailRelease:
    sent: list = field(default_factory=list)  # emails handed to the email job
    no_email: list = field(default_factory=list)  # recipients with no address on file
    gated: list = field(default_factory=list)  # held back by the resident gate


def _email_deliveries(action: DelinquencyAction):
    from homeoa.notify.models import Delivery

    return Delivery.all_associations.filter(
        association=action.association,
        notification__in=action.records.filter(
            channel=PreforeclosureRecord.Channel.EMAIL, notification__isnull=False
        ).values("notification"),
        channel=EMAIL,
    ).select_related("notification__recipient")


def email_copy_state(action: DelinquencyAction) -> dict | None:
    """The email copy's state for the case screen: None when the notice has no email copy."""
    from homeoa.notify.models import Delivery

    deliveries = list(_email_deliveries(action))
    if not deliveries:
        return None
    pending = [d for d in deliveries if d.status == Delivery.Status.PREVIEW]
    sent = [d.sent_at for d in deliveries if d.sent_at]
    return {
        "pending": len(pending),
        "sent_on": max(sent).date() if sent else None,
    }


@transaction.atomic
def release_email_copy(action: DelinquencyAction, user=None) -> EmailRelease:
    """Board action: send the statute's email copy of a notice the ladder stored as preview.

    The copy is forced (statute: by email whenever the address is known), so e-notice
    consent is not checked. The resident gate still holds: a non-board recipient's
    copy stays preview while residents are off; the email job checks it again at send.
    Idempotent: only ``preview`` deliveries move, so a second call does nothing.
    """
    from homeoa.core import residents_enabled
    from homeoa.notify.channels.email import address_for
    from homeoa.notify.models import Delivery
    from homeoa.notify.services import _dispatch, is_board

    association = action.association
    gate = not residents_enabled(association)
    out = EmailRelease()
    pending = (
        _email_deliveries(action)
        .filter(status=Delivery.Status.PREVIEW)
        .select_for_update(of=("self",))
    )
    for delivery in pending:
        person = delivery.notification.recipient
        if gate and not is_board(person, association):
            out.gated.append(person)
            continue
        if not address_for(person):
            out.no_email.append(person)
            continue
        delivery.status = Delivery.Status.QUEUED
        delivery.save(update_fields=["status"])
        _dispatch(delivery)
        out.sent.append(person)
    if out.sent:
        audit.record(
            "delinquency.email_copy_released",
            action.case,
            changes={
                "unit": action.case.unit.number,
                "step": action.step,
                "recipients": [p.pk for p in out.sent],
                "no_email": [p.pk for p in out.no_email],
            },
            actor=user,
        )
    return out


def set_contacts(association, contacts: dict, *, reason: str) -> None:
    """Save the foreclosure-assistance contacts for the statutory notice (audited)."""
    from homeoa.letters import statutory

    reason = _reason(reason)
    settings = dict(association.settings or {})
    old = settings.get(statutory.CONTACTS_SETTING)
    settings[statutory.CONTACTS_SETTING] = contacts
    association.settings = settings
    association.save(update_fields=["settings"])
    audit.record(
        "delinquency.contacts",
        association,
        changes={"old": old, "new": contacts, "reason": reason},
        message=reason,
    )


def set_offset(association, step: str, days, *, reason: str) -> None:
    """Board override of one step's offset (``None`` clears it back to the rule pack)."""
    key = f"delinquency.{step}_days"
    if days is None:
        rules.clear_override(association, key, reason=reason)
    else:
        rules.set_override(association, key, days, reason=reason)


# --- board to-dos -----------------------------------------------------------------------


def todo_items(association) -> list[dict]:
    """A demand letter waits for the board: review the draft and decide what to send."""
    pending = DelinquencyAction.all_associations.filter(
        association=association,
        step=Step.DEMAND,
        outcome=DelinquencyAction.Outcome.DONE,
        reviewed_at__isnull=True,
        case__status=DelinquencyCase.Status.OPEN,
    ).select_related("case__unit")
    return [
        {
            "id": f"delinquency-demand-{a.pk}",
            "kind": "money",
            "title": _("Review the demand letter for unit %(unit)s") % {"unit": a.case.unit.number},
            "sub": _("Draft only; nothing was sent. Oldest unpaid charge due %(date)s.")
            % {"date": oldest_due(a.case, a.as_of).isoformat()},
            "url": reverse("statements:delinquency"),
            "due": a.as_of,
            "post_url": reverse("statements:demand_reviewed", args=[a.pk]),
            "post_data": {},
            "action": _("Mark reviewed"),
        }
        for a in pending
    ] + statutory_todo_items(association)


def statutory_todo_items(association) -> list[dict]:
    """First-notice deadlines, missing assistance contacts, foreclosure threshold."""
    from homeoa.core.context import association_today
    from homeoa.letters import statutory

    as_of = association_today(association)
    cfg = statutory_config(association, as_of)
    if cfg is None:
        return []
    items = []
    if statutory.contacts_missing(association):
        items.append(
            {
                "id": "delinquency-contacts",
                "kind": "money",
                "title": _("Fill in the foreclosure-assistance contacts for delinquency notices"),
                "sub": _(
                    "The statutory notice lists the foreclosure hotline, HUD and civil legal "
                    "aid numbers; get them from the Department of Commerce."
                ),
                "url": reverse("statements:settings"),
                "action": _("Open settings"),
            }
        )
    cases = DelinquencyCase.all_associations.filter(
        association=association, status=DelinquencyCase.Status.OPEN
    ).select_related("unit")
    configured = offsets(association)
    for case in cases:
        items.extend(_translation_todos(case, as_of, configured, cfg))
        first = _done(case).get(Step.FIRST_NOTICE)
        if first is None or (_is_done(first) and not is_mailed(first)):
            deadline = first_notice_deadline(case, cfg)
            items.append(
                {
                    "id": f"delinquency-first-notice-{case.pk}",
                    "kind": "money",
                    "title": _("Mail the first notice of delinquency for unit %(unit)s by %(date)s")
                    % {"unit": case.unit.number, "date": deadline.isoformat()},
                    "sub": (
                        _("The notice is drafted in a mail batch; send it.")
                        if first is not None
                        else _("The ladder drafts it; check the delinquency page.")
                    ),
                    "url": reverse("statements:delinquency"),
                    "due": deadline,
                }
            )
        if foreclosure_ready(case, as_of, cfg):
            items.append(
                {
                    "id": f"delinquency-foreclosure-{case.pk}",
                    "kind": "money",
                    "title": _(
                        "Unit %(unit)s has reached the foreclosure threshold; foreclosure "
                        "needs a board vote and an attorney"
                    )
                    % {"unit": case.unit.number},
                    "sub": _("HomeOA takes no further action on its own."),
                    "url": reverse("statements:delinquency"),
                    "due": as_of,
                }
            )
    return items


def waiting_translation(case: DelinquencyCase, as_of: dt.date, configured, cfg) -> list[str]:
    """Missing languages when the case's next step is a statutory notice, else ``[]``."""
    if case.on_hold:
        return []
    nxt = statutory_next(case, as_of, configured, cfg)
    if nxt is None or nxt[0] not in NOTICE_STEPS:
        return []
    return missing_translations(case, as_of)


def _translation_todos(case, as_of, configured, cfg) -> list[dict]:
    from homeoa.registry.models import language_label

    missing = waiting_translation(case, as_of, configured, cfg)
    if not missing:
        return []
    template = _notice_template(case.association)
    first = _done(case).get(Step.FIRST_NOTICE)
    due = first_notice_deadline(case, cfg) if first is None else None
    nxt = statutory_next(case, as_of, configured, cfg)
    sub = (
        _("The notice is held until then. The deadline does not move: mail it by %(date)s.")
        % {"date": due.isoformat()}
        if due
        else _("The notice is held until then.")
    )
    return [
        {
            "id": f"delinquency-translation-{case.pk}-{lang}",
            "kind": "money",
            "title": _("Upload the %(lang)s translation of the %(step)s for unit %(unit)s")
            % {
                "lang": language_label(lang),
                "step": Step(nxt[0]).label.lower(),
                "unit": case.unit.number,
            },
            "sub": sub,
            "url": reverse("letters:edit", args=[template.pk]) + f"?language={lang}#translations",
            "action": _("Upload translation"),
            "due": due or as_of,
        }
        for lang in missing
    ]


def balance(association, unit_id: int, as_of: dt.date) -> Decimal:
    return overdue_by_unit(association, as_of).get(unit_id, {}).get("balance") or Decimal("0")
