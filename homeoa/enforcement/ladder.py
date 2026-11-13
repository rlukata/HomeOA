"""Violation escalation ladder (VIO-03): courtesy notice, then formal notice with a cure
deadline.

* Order: a notice of each kind is sent once; the formal notice needs a courtesy notice
  first when the rule pack says so (``enforcement.courtesy_notice_required``).
* The formal notice's cure deadline is ``sent_on + enforcement.cure_days`` (rule pack;
  Washington values are policy placeholders, unverified, since the statute leaves the
  procedure to the governing documents).
* Each notice is a letter from a board-editable template (``letters``), filed on the unit
  file. Delivery: when every current owner has fresh e-notice consent the notice
  goes by email; otherwise the letter goes into a draft mail batch for the board to send,
  and owners who did consent also get an email copy. Emails are created after commit
, so a failure never undoes the recorded step.
* Hearings and decisions (the later rungs) are in ``hearings``.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, rules
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from .models import Violation, ViolationStep
from .services import EnforcementError, schedule_in_force, set_status

NOTIFICATION_TYPE = "enforcement.violation_notice"
NOTICE_KINDS = (ViolationStep.Kind.COURTESY, ViolationStep.Kind.FORMAL)
TEMPLATE_KEYS = {
    ViolationStep.Kind.COURTESY: "violation-courtesy",
    ViolationStep.Kind.FORMAL: "violation-formal",
    ViolationStep.Kind.HEARING_NOTICE: "violation-hearing",
    ViolationStep.Kind.HEARING_CANCELLED: "violation-hearing-cancelled",
    ViolationStep.Kind.DECISION: "violation-decision",
}


def _templates():
    return {
        ViolationStep.Kind.COURTESY: {
            "name": _("Violation courtesy notice"),
            "subject": _("Courtesy notice: unit {{ unit.number }}"),
            "body": _(
                "Dear {{ owner.name }},\n\n"
                "On {{ violation.observed_on }} the board observed the following at unit "
                "{{ unit.number }}: {{ violation.category }}.\n\n"
                "{{ violation.description }}\n\n"
                "This is covered by {{ violation.citation }}. Please correct it. This is a "
                "courtesy notice; no fine has been imposed.\n\n"
                "If you have questions, contact the board"
                "{% if association.email %} at {{ association.email }}{% endif %}.\n\n"
                "Sincerely,\nThe Board of Directors\n{{ association.name }}"
            ),
        },
        ViolationStep.Kind.FORMAL: {
            "name": _("Violation formal notice"),
            "subject": _("Notice of violation: unit {{ unit.number }}"),
            "body": _(
                "Dear {{ owner.name }},\n\n"
                "This is formal notice of a violation at unit {{ unit.number }}: "
                "{{ violation.category }} ({{ violation.citation }}), observed on "
                "{{ violation.observed_on }}.\n\n"
                "{{ violation.description }}\n\n"
                "Please cure the violation by {{ violation.cure_by }}."
                "{% if violation.fine_amount %} Under the association's fine schedule, the "
                "fine for this violation is {{ violation.fine_amount }}.{% endif %} No fine "
                "is imposed without notice and an opportunity to be heard.\n\n"
                "[Board: check this wording against your declaration, bylaws and rules "
                "before sending.]\n\n"
                "Sincerely,\nThe Board of Directors\n{{ association.name }}"
            ),
        },
        ViolationStep.Kind.HEARING_NOTICE: {
            "name": _("Violation hearing notice"),
            "subject": _("Notice of hearing: unit {{ unit.number }}"),
            "body": _(
                "Dear {{ owner.name }},\n\n"
                "The board will hold a hearing on the violation at unit {{ unit.number }}: "
                "{{ violation.category }} ({{ violation.citation }}), observed on "
                "{{ violation.observed_on }}.\n\n"
                "Hearing: {{ violation.hearing_on }}, {{ violation.hearing_place }}.\n\n"
                "You may attend, speak and give the board any written statement or evidence "
                "before or at the hearing. The board will decide after the hearing whether "
                "a fine is imposed."
                "{% if violation.fine_amount %} The scheduled fine is "
                "{{ violation.fine_amount }}.{% endif %}\n\n"
                "[Board: check this wording against your declaration, bylaws and rules "
                "before sending.]\n\n"
                "Sincerely,\nThe Board of Directors\n{{ association.name }}"
            ),
        },
        ViolationStep.Kind.HEARING_CANCELLED: {
            "name": _("Violation hearing cancelled"),
            "subject": _("Hearing cancelled: unit {{ unit.number }}"),
            "body": _(
                "Dear {{ owner.name }},\n\n"
                "The hearing on {{ violation.hearing_on }} about the violation at unit "
                "{{ unit.number }} ({{ violation.category }}, {{ violation.citation }}) has "
                "been cancelled. You do not need to attend.\n\n"
                "{% if violation.cancel_reason %}Reason: {{ violation.cancel_reason }}\n\n"
                "{% endif %}"
                "If the board schedules a new hearing, you will receive a new hearing notice "
                "with the date and place. No fine is imposed without notice and an "
                "opportunity to be heard.\n\n"
                "[Board: check this wording against your declaration, bylaws and rules "
                "before sending.]\n\n"
                "Sincerely,\nThe Board of Directors\n{{ association.name }}"
            ),
        },
        ViolationStep.Kind.DECISION: {
            "name": _("Violation hearing decision"),
            "subject": _("Hearing decision: unit {{ unit.number }}"),
            "body": _(
                "Dear {{ owner.name }},\n\n"
                "After the hearing on {{ violation.hearing_on }} about the violation at unit "
                "{{ unit.number }} ({{ violation.category }}, {{ violation.citation }}), the "
                "board decided: {{ violation.decision }}.\n\n"
                "{% if violation.fine_imposed %}A fine of {{ violation.fine_imposed }} has "
                "been added to your account. Fines do not accrue late fees or interest.\n\n"
                "{% endif %}"
                "[Board: check this wording against your declaration, bylaws and rules "
                "before sending.]\n\n"
                "Sincerely,\nThe Board of Directors\n{{ association.name }}"
            ),
        },
    }


# --- registration (AppConfig.ready) -----------------------------------------------------


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="violations",
            key=NOTIFICATION_TYPE,
            label=gettext_lazy("Violation notice"),
            default_channels=(ch.EMAIL,),
            audience=ntypes.AUDIENCE_RESIDENTS,
            mandatory=True,
            subject_template="{{ subject }}",
            body_template="{% autoescape off %}{{ body }}{% endautoescape %}",
        )
    )


def register_merge_fields() -> None:
    from homeoa.letters import merge

    for name, label, sample in (
        ("category", gettext_lazy("Violation category"), "Pets"),
        ("citation", gettext_lazy("Violation rule citation"), "Rules §4.2"),
        ("observed_on", gettext_lazy("Violation observed date"), "September 1, 2026"),
        ("description", gettext_lazy("Violation description"), "Dog off leash"),
        ("cure_by", gettext_lazy("Cure deadline"), "October 15, 2026"),
        ("fine_amount", gettext_lazy("Fine schedule amount"), "$50.00"),
        ("hearing_on", gettext_lazy("Hearing date and time"), "October 30, 2026, 7 p.m."),
        ("hearing_place", gettext_lazy("Hearing place"), "Community room"),
        ("decision", gettext_lazy("Hearing decision"), "Fine"),
        ("cancel_reason", gettext_lazy("Hearing cancellation reason"), "Board member ill"),
        ("fine_imposed", gettext_lazy("Fine imposed"), "$50.00"),
    ):
        merge.register(
            f"violation.{name}",
            label,
            lambda ctx, n=name: ctx.extra.get(f"violation_{n}", ""),
            sample=sample,
            module="violations",
        )


# --- reading ----------------------------------------------------------------------------


def _date(d: dt.date | None) -> str:
    from django.utils.formats import date_format

    return date_format(d, "F j, Y") if d else ""


def fine_line(violation: Violation):
    schedule = schedule_in_force(violation.association, violation.observed_on)
    if schedule is None:
        return None
    return schedule.lines.filter(category=violation.category).first()


def merge_extra(violation: Violation, cure_by: dt.date | None) -> dict:
    from homeoa.letters.merge import money

    line = fine_line(violation)
    return {
        "violation_category": violation.category.name,
        "violation_citation": violation.category.rule_citation,
        "violation_observed_on": _date(violation.observed_on),
        "violation_description": violation.description,
        "violation_cure_by": _date(cure_by),
        "violation_fine_amount": money(line.first_amount) if line else "",
    }


def cure_days(association, on: dt.date):
    return rules.resolve(association, "enforcement.cure_days", on)


def courtesy_required(association, on: dt.date) -> bool:
    return bool(rules.value(association, "enforcement.courtesy_notice_required", on))


def sent_kinds(violation: Violation) -> set[str]:
    return set(violation.steps.values_list("kind", flat=True))


def notice_problem(violation: Violation, kind: str, on: dt.date) -> str:
    """Why ``kind`` cannot be sent now ("" when it can)."""
    if kind not in NOTICE_KINDS:
        return _("Unknown notice.")
    if violation.status != Violation.Status.OPEN:
        return _("Only an open violation gets a notice.")
    sent = sent_kinds(violation)
    if kind in sent:
        return _("This notice has already been sent.")
    if kind == ViolationStep.Kind.COURTESY and ViolationStep.Kind.FORMAL in sent:
        return _("A formal notice has already been sent.")
    if kind == ViolationStep.Kind.FORMAL:
        if ViolationStep.Kind.COURTESY not in sent and courtesy_required(violation.association, on):
            return _("Send the courtesy notice first: the rules require it.")
        if cure_days(violation.association, on).value is None:
            return _("Set the cure period (enforcement.cure_days) in the rules first.")
    return ""


def formal_step(violation: Violation) -> ViolationStep | None:
    return violation.steps.filter(kind=ViolationStep.Kind.FORMAL).first()


def cure_overdue(association, today: dt.date):
    """Open violations whose formal-notice cure deadline has passed and that have no
    hearing (a cancelled hearing does not count)."""
    from .models import Hearing

    live = Hearing.all_associations.filter(association=association, cancelled_on__isnull=True)
    return (
        Violation.all_associations.filter(
            association=association,
            status=Violation.Status.OPEN,
            steps__kind=ViolationStep.Kind.FORMAL,
            steps__cure_by__lt=today,
        )
        .exclude(pk__in=live.values("violation_id"))
        .distinct()
    )


# --- actions ----------------------------------------------------------------------------


def _template(association, kind: str):
    from homeoa.letters import services as letter_services
    from homeoa.letters.models import LetterTemplate

    key = TEMPLATE_KEYS[kind]
    found = LetterTemplate.all_associations.filter(
        association=association, key=key, current_version__isnull=False
    ).first()
    if found is not None:
        return found
    spec = _templates()[kind]
    return letter_services.create_template(
        association, key=key, draft_wording=True, **{k: str(v) for k, v in spec.items()}
    )


@transaction.atomic
def send_notice(violation: Violation, kind: str, *, user=None, today=None) -> ViolationStep:
    from homeoa.core.context import association_today

    violation = (
        Violation.all_associations.select_for_update()
        .select_related("unit", "category", "association")
        .get(pk=violation.pk)
    )
    association = violation.association
    today = today or association_today(association)
    problem = notice_problem(violation, kind, today)
    if problem:
        raise EnforcementError(problem)
    cure_by = None
    if kind == ViolationStep.Kind.FORMAL:
        cure_by = today + dt.timedelta(days=cure_days(association, today).value)
    extra = merge_extra(violation, cure_by)
    return deliver_step(violation, kind, extra=extra, today=today, user=user, cure_by=cure_by)


def deliver_step(
    violation: Violation, kind: str, *, extra: dict, today, user=None, cure_by=None, period=None
) -> ViolationStep:
    """Generate the step's letter, deliver it (email when every owner consented, else
    a draft mail batch plus email copies to those who did; emails after commit) and
    record the step. Call inside a transaction that holds the violation's row lock."""
    from homeoa.letters import batches, render
    from homeoa.letters import services as letter_services
    from homeoa.notify import services as nsvc
    from homeoa.registry.services import current_owners

    association = violation.association
    template = _template(association, kind)
    letter = letter_services.generate_letter(
        template, violation.unit, as_of=today, user=user, **extra
    )
    owners = current_owners(violation.unit, today)
    consenting = [p for p in owners if nsvc.email_allowed(p)]
    by_email = bool(owners) and len(consenting) == len(owners)
    batch = None
    if not by_email:
        batch = batches.create_batch(
            association,
            [letter],
            name=_("Violation notice: unit %(unit)s, %(date)s")
            % {"unit": violation.unit.number, "date": today.isoformat()},
            user=user,
        )
    actor = user if user is not None and user.is_authenticated else None
    step = ViolationStep.objects.create(
        association=association,
        violation=violation,
        kind=kind,
        sent_on=today,
        cure_by=cure_by,
        delivery=ViolationStep.Delivery.EMAIL if by_email else ViolationStep.Delivery.LETTER,
        letter=letter,
        batch=batch,
        emailed=len(consenting),
        created_by=actor,
    )
    if consenting:
        version = letter.template_version
        ctx = letter_services.context_for(association, violation.unit, today, **extra)
        _subject, body = render.render_letter(version.subject, version.body, ctx)
        # Email only to owners with fresh consent, after commit; never raises.
        nsvc.notify_after_commit(
            NOTIFICATION_TYPE,
            consenting,
            {"subject": letter.subject, "body": body},
            today,
            obj=letter,
            period=period or f"violation:{violation.pk}:{kind}",
            association=association,
            unit=violation.unit,
            force_channels=(ch.EMAIL,),
            attachments=[
                {
                    "name": f"{template.key}-{violation.unit.number}.pdf",
                    "content_type": "application/pdf",
                    "file_key": letter.file_key,
                }
            ],
        )
    audit.record(
        "enforcement.notice_sent",
        violation,
        actor=user,
        changes={
            "kind": kind,
            "sent_on": today.isoformat(),
            "cure_by": cure_by.isoformat() if cure_by else None,
            "delivery": step.delivery,
            "letter": letter.pk,
            "emailed": len(consenting),
        },
    )
    return step


@transaction.atomic
def mark_cured(violation: Violation, *, cured_on=None, note: str = "", user=None) -> None:
    from homeoa.core.context import association_today

    violation = Violation.all_associations.select_for_update().get(pk=violation.pk)
    cured_on = cured_on or association_today(violation.association)
    if violation.status != Violation.Status.OPEN:
        raise EnforcementError(_("Only an open violation can be marked cured."))
    if cured_on < violation.observed_on:
        raise EnforcementError(_("The cure date cannot be before the violation was observed."))
    set_status(violation, Violation.Status.CURED, note=note, user=user)
    violation.cured_on = cured_on
    violation.save(update_fields=["cured_on"])
    audit.record(
        "enforcement.cured",
        violation,
        actor=user,
        changes={"cured_on": cured_on.isoformat()},
        message=note[:300],
    )
