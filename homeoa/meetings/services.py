"""Meetings (GOV-03): scheduling, agenda, and the notice sent for each meeting.

Notice window
-------------
The window comes from rule-pack keys ``notices.<kind>.min_days`` / ``max_days`` (kind
``board_meeting``, ``owner_meeting`` or ``special_meeting``), never from code. The generic
pack carries placeholder policy defaults; a jurisdiction pack replaces them. A
notice may go out from ``date - max_days`` to ``date - min_days``; outside that it is
blocked with an explanation. The Home to-do "Send notice by <date>" is due on the last
allowed day.

Sending (``send_notice``)
-------------------------
* ``owners``: one notice per unit, to its current owners. Email when every owner has
  fresh e-notice consent, otherwise a letter in a mail batch handed to the postal
  provider (print batch or Lob). Each is filed in the unit's comm log through ``notify``.
* ``print``: a letter for every unit in a draft print batch (preview only): the board
  prints and mails it by hand. Allowed before cutover.
* ``board``: an email copy to each board member. Allowed before cutover.

Emergency board meetings
--------------------------------
A board meeting called for an unforeseeable event carries ``emergency`` and a required
reason (audited). Its window uses ``notices.board_meeting.emergency_min_days`` instead of
``min_days``, and notice goes out electronically only: no print batch and no letters.
When the rule pack sets ``notices.board_meeting.emergency_to_known_addresses`` (WA:
RCW 64.90.445(2)(f)), every owner with an email on file is emailed, consent or not, and
units with no email are counted as no_email. Otherwise only units whose owners all
consented are emailed, and the rest are counted as no_consent.

``owners`` needs the association setting ``owner_notices_live`` (default off) and
residents switched on. Notices and side effects run after commit; every notice is
kept as a ``MeetingNotice`` with its text, letters as stored PDFs. A repeat send
skips units (or board members) already noticed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db import transaction
from django.utils.formats import date_format, time_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, rules
from homeoa.core import residents_enabled
from homeoa.core.context import association_today
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes
from homeoa.rules.keys import KEYS

from .models import AgendaItem, Meeting, MeetingNotice

NOTIFICATION_TYPE = "meetings.notice"
TEMPLATE_KEY = "meeting-notice"
LIVE_SETTING = "owner_notices_live"

MODE_OWNERS = "owners"
MODE_PRINT = "print"
MODE_BOARD = "board"
MODES = (MODE_OWNERS, MODE_PRINT, MODE_BOARD)

RULE_KIND = {
    Meeting.Kind.BOARD: "board_meeting",
    Meeting.Kind.ANNUAL: "owner_meeting",
    Meeting.Kind.SPECIAL: "special_meeting",
}


class MeetingError(Exception):
    pass


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=NOTIFICATION_TYPE,
            label=gettext_lazy("Meeting notice"),
            default_channels=(ch.EMAIL,),
            audience=ntypes.AUDIENCE_ALL,
            mandatory=True,
            subject_template="{{ subject }}",
            body_template="{{ body }}",
        )
    )


# --- scheduling -------------------------------------------------------------------------


def create_meeting(association, *, kind, date, user=None, **fields) -> Meeting:
    if kind not in Meeting.Kind.values:
        raise MeetingError(_("Choose a meeting kind."))
    emergency = bool(fields.pop("emergency", False))
    reason = (fields.pop("emergency_reason", "") or "").strip()
    if emergency:
        _check_emergency(kind, reason)
    meeting = Meeting.objects.create(
        association=association,
        kind=kind,
        date=date,
        created_by=user if user is not None and user.is_authenticated else None,
        **fields,
    )
    if emergency:
        set_emergency(meeting, True, reason, user=user)
    return meeting


def _check_emergency(kind: str, reason: str) -> None:
    if kind != Meeting.Kind.BOARD:
        raise MeetingError(_("Only a board meeting can be an emergency meeting."))
    if not reason:
        raise MeetingError(_("Describe the unforeseeable event that requires the meeting."))


def set_emergency(meeting: Meeting, on: bool, reason: str = "", *, user=None) -> Meeting:
    """Mark (or unmark) a draft board meeting as an emergency meeting, with its reason."""
    reason = (reason or "").strip()
    if meeting.status != Meeting.Status.DRAFT:
        raise MeetingError(_("Only a meeting whose notice has not gone out can change this."))
    if on:
        _check_emergency(meeting.kind, reason)
    else:
        reason = ""
    meeting.emergency = bool(on)
    meeting.emergency_reason = reason
    meeting.save(update_fields=["emergency", "emergency_reason"])
    audit.record(
        "meeting.emergency",
        meeting,
        changes={"emergency": bool(on), "reason": reason},
        actor=user if user is not None and user.is_authenticated else None,
    )
    return meeting


EDITABLE = ("title", "date", "start_time", "location", "video_url", "dial_in", "description")


def update_meeting(meeting: Meeting, **fields) -> Meeting:
    if not meeting.is_open:
        raise MeetingError(_("A held or cancelled meeting cannot be changed."))
    if meeting.status == Meeting.Status.NOTICED and (
        fields.get("date", meeting.date) != meeting.date
        or fields.get("start_time", meeting.start_time) != meeting.start_time
    ):
        raise MeetingError(
            _("Notice has gone out for this date. Cancel it and schedule a new meeting instead.")
        )
    for name, value in fields.items():
        if name in EDITABLE:
            setattr(meeting, name, value)
    meeting.save()
    return meeting


def set_status(meeting: Meeting, status: str) -> Meeting:
    allowed = {
        Meeting.Status.HELD: (Meeting.Status.DRAFT, Meeting.Status.NOTICED),
        Meeting.Status.CANCELLED: (Meeting.Status.DRAFT, Meeting.Status.NOTICED),
    }
    if meeting.status not in allowed.get(status, ()):
        raise MeetingError(_("That status change is not allowed."))
    meeting.status = status
    meeting.save(update_fields=["status"])
    return meeting


def add_agenda_item(meeting: Meeting, *, title, notes="", resolution=None) -> AgendaItem:
    if not meeting.is_open:
        raise MeetingError(_("A held or cancelled meeting cannot be changed."))
    if resolution is not None and resolution.association_id != meeting.association_id:
        raise MeetingError("resolution from another association")
    last = meeting.agenda.order_by("-position").values_list("position", flat=True).first()
    return AgendaItem.objects.create(
        association=meeting.association,
        meeting=meeting,
        position=(last or 0) + 1,
        title=title,
        notes=notes,
        resolution=resolution,
    )


def move_agenda_item(item: AgendaItem, direction: int) -> None:
    items = list(item.meeting.agenda.all())
    i = next(n for n, x in enumerate(items) if x.pk == item.pk)
    j = i + direction
    if not 0 <= j < len(items):
        return
    items[i], items[j] = items[j], items[i]
    for n, x in enumerate(items, start=1):
        if x.position != n:
            x.position = n
            x.save(update_fields=["position"])


def remove_agenda_item(item: AgendaItem) -> None:
    if not item.meeting.is_open:
        raise MeetingError(_("A held or cancelled meeting cannot be changed."))
    item.delete()


# --- notice window ----------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    min_rule: rules.Resolved
    max_rule: rules.Resolved | None  # None: the pack has no earliest-date key
    earliest: dt.date | None  # first day notice may go out (None: no limit)
    latest: dt.date  # last day notice may go out


def is_emergency(meeting: Meeting) -> bool:
    return meeting.emergency and meeting.kind == Meeting.Kind.BOARD


def notice_window(meeting: Meeting, as_of: dt.date | None = None) -> Window:
    kind = RULE_KIND[meeting.kind]
    a = meeting.association
    min_key = "emergency_min_days" if is_emergency(meeting) else "min_days"
    min_rule = rules.resolve(a, f"notices.{kind}.{min_key}", as_of)
    max_key = f"notices.{kind}.max_days"
    max_rule = rules.resolve(a, max_key, as_of) if max_key in KEYS else None
    min_days = min_rule.value or 0
    earliest = (
        meeting.date - dt.timedelta(days=max_rule.value)
        if max_rule is not None and max_rule.is_set
        else None
    )
    return Window(
        min_rule=min_rule,
        max_rule=max_rule,
        earliest=earliest,
        latest=meeting.date - dt.timedelta(days=min_days),
    )


def window_problem(meeting: Meeting, on: dt.date) -> str:
    """Why notice cannot go out on ``on`` ("" when it can)."""
    w = notice_window(meeting, on)
    if w.earliest is not None and on < w.earliest:
        return _(
            "Too early: notice may go out no more than %(days)s days before the meeting, "
            "so from %(date)s."
        ) % {"days": w.max_rule.value, "date": date_format(w.earliest, "M j, Y")}
    if on > w.latest:
        return _(
            "Too late: notice must go out at least %(days)s days before the meeting, "
            "by %(date)s. Move the meeting to a later date."
        ) % {"days": w.min_rule.value or 0, "date": date_format(w.latest, "M j, Y")}
    return ""


# --- notice text ------------------------------------------------------------------------


def when_text(meeting: Meeting) -> str:
    when = date_format(meeting.date, "l, F j, Y")
    if meeting.start_time:
        when = f"{when}, {time_format(meeting.start_time, 'g:i A')}"
    return when


def where_text(meeting: Meeting) -> str:
    parts = [p for p in (meeting.location, meeting.video_url) if p]
    if meeting.dial_in:
        parts.append(_("by phone: %(dial_in)s") % {"dial_in": meeting.dial_in})
    return " / ".join(parts) or _("To be announced")


def is_remote(meeting: Meeting) -> bool:
    return bool(meeting.video_url or meeting.dial_in)


def owner_comment_text(meeting: Meeting) -> str:
    """The fixed first agenda item of a board meeting when the rule pack sets an owner
    comment period (``meetings.board.owner_comment_minutes``; WA: RCW 64.90.445(2)(e))."""
    if meeting.kind != Meeting.Kind.BOARD:
        return ""
    minutes = rules.value(meeting.association, "meetings.board.owner_comment_minutes", meeting.date)
    if not minutes:
        return ""
    return _(
        "Owner comments on agenda items (at least %(minutes)s minutes, before the board votes)"
    ) % {"minutes": minutes}


def remote_problem(meeting: Meeting) -> str:
    """Why a meeting held by video can't be noticed yet ("" when it can): the rule pack
    may require a telephone option (``meetings.remote.phone_option_required``)."""
    if not meeting.video_url or meeting.dial_in:
        return ""
    rule = rules.resolve(meeting.association, "meetings.remote.phone_option_required", meeting.date)
    if not rule.value:
        return ""
    return _(
        "Add a way to join by phone: anyone entitled to attend a meeting held by video must "
        "be able to join by telephone (%(cite)s)."
    ) % {"cite": rule.citation}


def agenda_text(meeting: Meeting) -> str:
    titles = [item.title for item in meeting.agenda.all()]
    if comment := owner_comment_text(meeting):
        titles.insert(0, comment)
    lines = [f"{n}. {title}" for n, title in enumerate(titles, start=1)]
    return "\n".join(lines) or _("The agenda will follow.")


def merge_extra(meeting: Meeting) -> dict:
    return {
        "meeting_title": meeting.display_title,
        "meeting_when": when_text(meeting),
        "meeting_where": where_text(meeting),
        "meeting_agenda": agenda_text(meeting),
    }


def notice_text(meeting: Meeting) -> tuple[str, str]:
    a = meeting.association
    subject = _("Notice of %(meeting)s on %(date)s") % {
        "meeting": meeting.display_title,
        "date": date_format(meeting.date, "M j, Y"),
    }
    body = _(
        "%(association)s gives notice of the %(meeting)s.\n\n"
        "When: %(when)s\nWhere: %(where)s\n\nAgenda:\n%(agenda)s"
    ) % {
        "association": a.name,
        "meeting": meeting.display_title,
        "when": when_text(meeting),
        "where": where_text(meeting),
        "agenda": agenda_text(meeting),
    }
    if (
        meeting.kind == Meeting.Kind.BOARD
        and is_remote(meeting)
        and rules.value(a, "meetings.remote.board_votes_by_roll_call", meeting.date)
    ):
        body = f"{body}\n\n" + _("Board votes at this meeting are taken by roll call.")
    if is_emergency(meeting):
        body = _(
            "This is an emergency meeting of the board, called for an unforeseeable event: "
            "%(reason)s\n\n%(body)s"
        ) % {"reason": meeting.emergency_reason, "body": body}
    if meeting.description:
        body = f"{body}\n\n{meeting.description}"
    return subject, body


def register_merge_fields() -> None:
    from homeoa.letters import merge

    for name, label, sample in (
        ("title", gettext_lazy("Meeting title"), "Annual owners meeting"),
        ("when", gettext_lazy("Meeting date and time"), "Thursday, December 3, 2026, 7:00 PM"),
        ("where", gettext_lazy("Meeting place or video link"), "Clubhouse"),
        ("agenda", gettext_lazy("Meeting agenda"), "1. Budget ratification"),
    ):
        merge.register(
            f"meeting.{name}",
            label,
            lambda ctx, n=name: ctx.extra.get(f"meeting_{n}", ""),
            sample=sample,
        )


def letter_template(association):
    from homeoa.letters import services as letter_services
    from homeoa.letters.models import LetterTemplate

    template = LetterTemplate.all_associations.filter(
        association=association, key=TEMPLATE_KEY
    ).first()
    if template is not None:
        return template
    return letter_services.create_template(
        association,
        key=TEMPLATE_KEY,
        name=_("Meeting notice"),
        subject=_("Notice of {{ meeting.title }}"),
        body=_(
            "Dear {{ owner.name }},\n\n"
            "{{ association.name }} gives notice of the {{ meeting.title }}.\n\n"
            "When: {{ meeting.when }}\n"
            "Where: {{ meeting.where }}\n\n"
            "Agenda:\n{{ meeting.agenda }}\n\n"
            "Sincerely,\nThe Board of Directors\n{{ association.name }}"
        ),
        draft_wording=True,
    )


# --- sending ----------------------------------------------------------------------------


def owner_notices_live(association) -> bool:
    """Notices to owners go out only once the board switches this on after cutover."""
    return bool((association.settings or {}).get(LIVE_SETTING, False))


def set_owner_notices_live(association, on: bool, *, user=None) -> None:
    association.settings = {**(association.settings or {}), LIVE_SETTING: bool(on)}
    association.save(update_fields=["settings"])
    audit.record(
        "meetings.owner_notices_live", association, changes={LIVE_SETTING: bool(on)}, actor=user
    )


def mode_problem(association, mode: str, meeting: Meeting | None = None) -> str:
    if mode not in MODES:
        return _("Choose how to send the notice.")
    if mode == MODE_PRINT and meeting is not None and is_emergency(meeting):
        return _("Emergency meeting notice goes out by email only, not by letter.")
    if mode == MODE_OWNERS:
        if not owner_notices_live(association):
            return _(
                "Owner notices are not switched on yet (before cutover). Send a board copy "
                "or a print-only batch to mail by hand."
            )
        if not residents_enabled(association):
            return _("Residents are not switched on yet, so nothing can be sent to owners.")
    return ""


@dataclass
class SendResult:
    email: int = 0
    mail: int = 0
    printed: int = 0
    board: int = 0
    skipped: int = 0
    no_consent: int = 0  # emergency notice: units without e-notice consent
    no_email: int = 0  # emergency notice to known addresses: units with no email on file
    batch: object = None

    @property
    def total(self) -> int:
        return self.email + self.mail + self.printed + self.board


@transaction.atomic
def send_notice(meeting: Meeting, mode: str, *, user=None, today=None) -> SendResult:
    from homeoa.letters import batches
    from homeoa.letters import services as letter_services
    from homeoa.notify import services as nsvc
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_owners

    meeting = Meeting.all_associations.select_for_update().get(pk=meeting.pk)
    association = type(meeting.association).objects.get(pk=meeting.association_id)
    today = today or association_today(association)
    if not meeting.is_open:
        raise MeetingError(_("A held or cancelled meeting gets no notice."))
    problem = mode_problem(association, mode, meeting)
    if not problem and mode != MODE_BOARD:
        problem = window_problem(meeting, today) or remote_problem(meeting)
    if problem:
        raise MeetingError(problem)

    actor = user if user is not None and user.is_authenticated else None
    subject, body = notice_text(meeting)
    result = SendResult()
    common = {
        "meeting": meeting,
        "association": association,
        "subject": subject[:300],
        "body": body,
        "sent_on": today,
        "sent_by": actor,
    }

    def _email(people, unit=None):
        nsvc.notify_after_commit(
            NOTIFICATION_TYPE,
            people,
            {"subject": subject, "body": body},
            today,
            obj=meeting,
            period="notice",
            association=association,
            unit=unit,
            force_channels=(ch.EMAIL,),
        )

    if mode == MODE_BOARD:
        noticed = set(
            meeting.notices.filter(person__isnull=False).values_list("person_id", flat=True)
        )
        for person in nsvc.board_recipients(association):
            if person.pk in noticed or not person.email:
                result.skipped += 1
                continue
            _email([person])
            MeetingNotice.objects.create(
                person=person, channel=MeetingNotice.Channel.BOARD, **common
            )
            result.board += 1
    else:
        noticed = set(meeting.notices.filter(unit__isnull=False).values_list("unit_id", flat=True))
        units = sorted(
            Unit.all_associations.filter(association=association), key=lambda u: u.sort_key
        )
        template = None
        letters: list[tuple[object, object]] = []
        to_known = (
            mode == MODE_OWNERS
            and is_emergency(meeting)
            and bool(rules.value(association, "notices.board_meeting.emergency_to_known_addresses"))
        )
        for unit in units:
            owners = current_owners(unit, today)
            if unit.pk in noticed or not owners:
                result.skipped += 1
                continue
            if to_known:  # statute: every known email, whatever the e-notice consent
                known = [p for p in owners if p.email]
                if known:
                    _email(known, unit)
                    MeetingNotice.objects.create(
                        unit=unit, channel=MeetingNotice.Channel.EMAIL, **common
                    )
                    result.email += 1
                else:
                    result.no_email += 1
                continue
            if mode == MODE_OWNERS and all(nsvc.email_allowed(p) for p in owners):
                _email(owners, unit)
                MeetingNotice.objects.create(
                    unit=unit, channel=MeetingNotice.Channel.EMAIL, **common
                )
                result.email += 1
                continue
            if is_emergency(meeting):
                result.no_consent += 1  # Electronic only, no letter
                continue
            template = template or letter_template(association)
            letter = letter_services.generate_letter(
                template, unit, as_of=today, user=user, **merge_extra(meeting)
            )
            channel = (
                MeetingNotice.Channel.MAIL if mode == MODE_OWNERS else MeetingNotice.Channel.PRINT
            )
            notice = MeetingNotice.objects.create(
                unit=unit, channel=channel, letter=letter, **common
            )
            letters.append((notice, letter))
        if letters:
            batch = batches.create_batch(
                association,
                [letter for _n, letter in letters],
                name=_("Notice: %(meeting)s") % {"meeting": meeting.display_title},
                user=user,
                preview_only=mode == MODE_PRINT,
            )
            if mode == MODE_OWNERS:
                batches.send_batch(batch, user=user)
                result.mail = len(letters)
            else:
                result.printed = len(letters)
            MeetingNotice.all_associations.filter(pk__in=[n.pk for n, _l in letters]).update(
                batch=batch
            )
            result.batch = batch
        if result.total and meeting.status == Meeting.Status.DRAFT:
            meeting.status = Meeting.Status.NOTICED
            meeting.noticed_on = today
            meeting.save(update_fields=["status", "noticed_on"])

    audit.record(
        "meeting.notice",
        meeting,
        changes={
            "mode": mode,
            "email": result.email,
            "mail": result.mail,
            "print": result.printed,
            "board": result.board,
            "skipped": result.skipped,
            "no_consent": result.no_consent,
            "no_email": result.no_email,
            "emergency": is_emergency(meeting),
        },
        actor=user,
    )
    return result


# --- lists ------------------------------------------------------------------------------


def upcoming_unnoticed(association, today: dt.date):
    return Meeting.all_associations.filter(
        association=association, status=Meeting.Status.DRAFT, date__gte=today
    ).order_by("date", "pk")
