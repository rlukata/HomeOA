"""Minutes, attendance and proxies (GOV-04, GOV-05).

Minutes state machine
---------------------
``draft`` (editable) -> ``approved`` (records the approving meeting and date; the text is
frozen, enforced in ``Minutes.save``) -> ``published`` (a PDF filed with
``documents.attach`` under Meetings, visible to owners, or owners and tenants if chosen).
After publishing the board may send the PDF to the secretary for signature through
``esign``; the signed copy becomes the document's next version.

Attendance and proxies
----------------------
Owners meetings record attendance per unit, board meetings per board member (in person
or remote). A proxy names the grantor unit, the holder and its date, with an optional
uploaded copy; it can be revoked until the meeting date. Quorum: ``quorum.compute``.
"""

from __future__ import annotations

import datetime as dt
import io
from xml.sax.saxutils import escape

from django.db import transaction
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa import audit
from homeoa.core.context import association_today
from homeoa.documents import services as docs
from homeoa.documents.models import Category, Visibility
from homeoa.governance import services as gov
from homeoa.governance.models import Office
from homeoa.registry.models import Person, Unit

from .models import Attendance, Meeting, Minutes, Proxy
from .services import MeetingError, when_text, where_text

PUBLISH_VISIBILITIES = (Visibility.OWNERS, Visibility.TENANTS)
ON_SIGNED = "homeoa.meetings.minutes.on_signed"


# --- minutes ----------------------------------------------------------------------------


def get_or_create(meeting: Meeting, *, user=None) -> Minutes:
    minutes = Minutes.all_associations.filter(meeting=meeting).first()
    if minutes is None:
        minutes = Minutes.all_associations.create(
            association=meeting.association,
            meeting=meeting,
            drafted_by=user if getattr(user, "is_authenticated", False) else None,
        )
    return minutes


def save_draft(minutes: Minutes, body: str, *, user=None) -> Minutes:
    if not minutes.is_draft:
        raise MeetingError(_("Approved minutes cannot be changed."))
    minutes.body = body
    if getattr(user, "is_authenticated", False):
        minutes.drafted_by = user
    minutes.save()
    return minutes


def approve(minutes: Minutes, *, at_meeting: Meeting, on: dt.date, user=None) -> Minutes:
    if not minutes.is_draft:
        raise MeetingError(_("These minutes are already approved."))
    if not minutes.body.strip():
        raise MeetingError(_("Write the minutes before approving them."))
    if at_meeting.association_id != minutes.association_id:
        raise MeetingError("meeting from another association")
    if at_meeting.pk == minutes.meeting_id or at_meeting.date < minutes.meeting.date:
        raise MeetingError(_("Minutes are approved at a later meeting."))
    if on < at_meeting.date:
        raise MeetingError(_("The approval date cannot be before the approving meeting."))
    minutes.status = Minutes.Status.APPROVED
    minutes.approved_at = at_meeting
    minutes.approved_on = on
    minutes.save()
    audit.record(
        "meetings.minutes_approved",
        minutes,
        actor=user,
        changes={"approved_at": at_meeting.pk, "approved_on": on.isoformat()},
    )
    return minutes


def render_pdf(minutes: Minutes) -> bytes:
    # Fonts (DejaVu Sans) are registered by importing the letters PDF module.
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    from homeoa.letters.pdf import FONT, FONT_BOLD

    meeting = minutes.meeting
    body = ParagraphStyle("b", fontName=FONT, fontSize=11, leading=15, spaceAfter=8)
    head = ParagraphStyle("h", fontName=FONT_BOLD, fontSize=15, leading=19, spaceAfter=6)
    small = ParagraphStyle("s", fontName=FONT, fontSize=9, leading=12, spaceAfter=4)
    title = _("Minutes: %(meeting)s") % {"meeting": meeting.display_title}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=inch,
        bottomMargin=inch,
        title=title,
        author=minutes.association.name,
        creator="HomeOA",
    )
    story = [
        Paragraph(escape(minutes.association.name), small),
        Paragraph(escape(title), head),
        Paragraph(escape(f"{when_text(meeting)} · {where_text(meeting)}"), small),
        Paragraph(
            escape(
                _("Approved at %(meeting)s on %(date)s.")
                % {
                    "meeting": minutes.approved_at.display_title,
                    "date": date_format(minutes.approved_on, "F j, Y"),
                }
            ),
            small,
        ),
        Spacer(1, 10),
    ]
    for para in minutes.body.replace("\r\n", "\n").split("\n\n"):
        if para.strip():
            story.append(Paragraph(escape(para.strip()).replace("\n", "<br/>"), body))
    doc.build(story)
    return buf.getvalue()


def publish(minutes: Minutes, *, visibility: str = Visibility.OWNERS, user=None) -> Minutes:
    if minutes.status != Minutes.Status.APPROVED:
        raise MeetingError(_("Only approved minutes can be published."))
    if visibility not in PUBLISH_VISIBILITIES:
        raise MeetingError(_("Publish to owners, or to owners and tenants."))
    meeting = minutes.meeting
    with transaction.atomic():
        version = docs.attach(
            minutes.association,
            filename=f"minutes-{meeting.date.isoformat()}.pdf",
            content=render_pdf(minutes),
            title=_("Minutes: %(meeting)s, %(date)s")
            % {"meeting": meeting.display_title, "date": date_format(meeting.date, "M j, Y")},
            category=Category.MINUTES,
            visibility=visibility,
            effective_date=minutes.approved_on,
            user=user,
        )
        minutes.document = version.document
        minutes.status = Minutes.Status.PUBLISHED
        minutes.published_on = association_today(minutes.association)
        minutes.save()
    audit.record("meetings.minutes_published", minutes, actor=user)
    return minutes


def secretary(association, on: dt.date) -> Person | None:
    for term in gov.roster(association, on):
        if term.office == Office.SECRETARY:
            return term.person
    return None


def send_for_signature(minutes: Minutes, *, user=None, base_url: str = ""):
    from homeoa.esign import services as esign

    if minutes.status != Minutes.Status.PUBLISHED or minutes.document is None:
        raise MeetingError(_("Publish the minutes before sending them for signature."))
    if minutes.envelope is not None and minutes.envelope.is_open:
        raise MeetingError(_("These minutes are already out for signature."))
    person = secretary(minutes.association, association_today(minutes.association))
    if person is None or not person.email:
        raise MeetingError(_("Add a secretary with an email address to the board first."))
    version = minutes.document.current_version
    env = esign.create_envelope(
        minutes.association,
        title=minutes.document.title,
        source_key=version.file_key,
        source_name=version.file_name,
        signers=[{"name": person.full_name, "email": person.email, "user": person.user}],
        on_complete=ON_SIGNED,
        on_complete_ref=str(minutes.pk),
        user=user,
    )
    esign.send(env, base_url=base_url)
    minutes.envelope = env
    minutes.save(update_fields=["envelope", "updated_at"])
    return env


def on_signed(envelope) -> None:
    """esign callback: file the signed PDF as the minutes document's next version."""
    from homeoa.providers import get_provider

    minutes = Minutes.all_associations.filter(
        pk=envelope.on_complete_ref, association=envelope.association
    ).first()
    if minutes is None or minutes.document is None:
        return
    content = get_provider("FileStorage", envelope.association).get(envelope.result_key)
    docs.attach(
        envelope.association,
        filename=minutes.document.current_version.file_name.replace(".pdf", "-signed.pdf"),
        content=content,
        document=minutes.document,
        note=_("Signed by the secretary"),
    )


# --- attendance -------------------------------------------------------------------------


def set_attendance(meeting: Meeting, *, mode: str, unit: Unit | None = None, person=None):
    """Record (or with ``mode=""`` clear) attendance for a unit or a board member."""
    if meeting.status == Meeting.Status.CANCELLED:
        raise MeetingError(_("The meeting was cancelled."))
    if meeting.is_owner_meeting:
        if unit is None or unit.association_id != meeting.association_id:
            raise MeetingError(_("Choose a unit."))
        key = {"unit": unit}
    else:
        if person is None or person.association_id != meeting.association_id:
            raise MeetingError(_("Choose a board member."))
        key = {"person": person}
    if not mode:
        Attendance.all_associations.filter(meeting=meeting, **key).delete()
        return None
    if mode not in Attendance.Mode.values:
        raise MeetingError(_("Choose how they attended."))
    row, _created = Attendance.all_associations.update_or_create(
        meeting=meeting,
        association=meeting.association,
        **key,
        defaults={"mode": mode},
    )
    return row


# --- proxies ----------------------------------------------------------------------------


def add_proxy(
    meeting: Meeting,
    *,
    unit: Unit,
    holder: str,
    granted_on: dt.date,
    upload: tuple[str, bytes] | None = None,
    user=None,
) -> Proxy:
    if not meeting.is_owner_meeting:
        raise MeetingError(_("Proxies are for owners meetings."))
    if unit.association_id != meeting.association_id:
        raise MeetingError(_("Choose a unit."))
    if not holder.strip():
        raise MeetingError(_("Enter who holds the proxy."))
    if granted_on > meeting.date:
        raise MeetingError(_("A proxy must be dated on or before the meeting."))
    if Proxy.all_associations.filter(meeting=meeting, unit=unit, revoked_on__isnull=True).exists():
        raise MeetingError(_("This unit already has a proxy. Revoke it first."))
    with transaction.atomic():
        version = None
        if upload is not None:
            name, content = upload
            version = docs.attach(
                meeting.association,
                filename=name,
                content=content,
                title=_("Proxy: unit %(unit)s, %(meeting)s")
                % {"unit": unit.number, "meeting": meeting.display_title},
                category=Category.MINUTES,
                visibility=Visibility.BOARD,
                user=user,
            )
        proxy = Proxy.all_associations.create(
            association=meeting.association,
            meeting=meeting,
            unit=unit,
            holder=holder.strip()[:200],
            granted_on=granted_on,
            document=version,
        )
    audit.record("meetings.proxy_added", proxy, actor=user)
    return proxy


def revoke_proxy(proxy: Proxy, *, on: dt.date, user=None) -> Proxy:
    if proxy.revoked_on is not None:
        raise MeetingError(_("This proxy is already revoked."))
    if on > proxy.meeting.date or proxy.meeting.status == Meeting.Status.HELD:
        raise MeetingError(_("A proxy can only be revoked before the meeting."))
    proxy.revoked_on = on
    proxy.save(update_fields=["revoked_on"])
    audit.record("meetings.proxy_revoked", proxy, actor=user)
    return proxy
