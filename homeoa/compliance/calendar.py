"""Compliance calendar (GOV-10): pack obligation periods, manual items and vendor
dates on one list; reminders before each deadline; a board-only community calendar source.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit
from homeoa.core.context import association_today
from homeoa.notify import types as ntypes

from .models import ManualItem, ObligationPeriod, PeriodStatus, Recurrence, add_months

REMINDER_TYPE = "compliance.reminder"
OFFSETS_KEY = "compliance_reminder_days"
DEFAULT_OFFSETS = (30, 7)
SOURCE_KEY = "compliance"


class CalendarError(ValueError):
    pass


# ---------------------------------------------------------------- suggestions

# Prefills for the "add item" form; the board confirms every date before saving.
SUGGESTIONS = {
    "1099": (gettext_lazy("File 1099s for vendors"), (1, 31), Recurrence.YEARLY),
    "1120h": (gettext_lazy("File federal Form 1120-H"), (4, 15), Recurrence.YEARLY),
    "annual_report": (gettext_lazy("State annual report"), None, Recurrence.YEARLY),
}


def suggestion(key: str, today: dt.date) -> dict:
    """Initial form values for a suggestion; the next such date on or after ``today``."""
    if key not in SUGGESTIONS:
        return {}
    title, md, recurrence = SUGGESTIONS[key]
    out = {"title": str(title), "recurrence": recurrence}
    if md:
        day = dt.date(today.year, *md)
        out["anchor"] = day if day >= today else dt.date(today.year + 1, *md)
    return out


# ---------------------------------------------------------------- manual items


@transaction.atomic
def save_item(item: ManualItem, user=None) -> ManualItem:
    """Save an item; ``next_due`` is always recomputed from ``anchor`` so editing the first
    due date (or the recurrence) after occurrences were done keeps the series aligned:
    the same number of occurrences stays done; if the recurrence changed, the next open
    occurrence is the first one after the last completion. A first due date moved to or
    past the open occurrence restarts the series there."""
    old = ManualItem.objects.filter(pk=item.pk).first() if item.pk else None
    if old is None or not old.active or item.anchor >= old.next_due:
        item.next_due = item.first_due_after(item.last_done_on)
    elif old.recurrence == item.recurrence and item.recurrence != Recurrence.ONCE:
        item.next_due = add_months(item.anchor, old._offset())
    else:
        item.next_due = item.first_due_after(item.last_done_on)
    item.save()
    return item


@transaction.atomic
def mark_done(item: ManualItem, user=None, *, on: dt.date | None = None, upload=None) -> ManualItem:
    """Record the current occurrence as done and move on to the next one.

    ``upload`` (a Django ``UploadedFile``, optional) is filed in the document library
    (board-only) as proof and linked from the item."""
    if not item.active:
        raise CalendarError(_("This item is no longer active."))
    item.last_done_on = on or association_today(item.association)
    item.last_done_by = user
    item.last_done_document = None
    if upload is not None:
        from homeoa.documents import services as docs

        try:
            version = docs.attach(
                item.association,
                filename=upload.name,
                content=upload.read(),
                title=_("%(title)s (done %(date)s)")
                % {"title": item.title, "date": item.last_done_on.isoformat()},
                user=user,
            )
        except docs.UploadRejected as exc:
            raise CalendarError(" ".join(exc.messages)) from exc
        item.last_done_document = version.document
    following = item.following()
    if following is None:
        item.active = False
    else:
        item.next_due = following
    item.save(
        update_fields=["last_done_on", "last_done_by", "last_done_document", "active", "next_due"]
    )
    return item


# ---------------------------------------------------------------- entries


@dataclass(frozen=True)
class Entry:
    """One dated row on the compliance calendar."""

    uid: str
    kind: str  # "obligation", "manual", "coi", "contract"
    title: str
    due: dt.date
    url: str
    detail: str = ""
    done: bool = False
    read_only: bool = False

    def overdue(self, today: dt.date) -> bool:
        return not self.done and self.due < today


def _period_entries(association, start, end) -> list[Entry]:
    rows = ObligationPeriod.all_associations.filter(
        association=association, due_date__gte=start, due_date__lte=end
    )
    return [
        Entry(
            uid=f"compliance-period-{p.pk}.a{association.pk}@homeoa",
            kind="obligation",
            title=f"{p.title} ({p.year})",
            due=p.due_date,
            url=reverse("compliance:period", args=[p.pk]),
            detail=p.citation,
            done=p.status != PeriodStatus.OPEN,
        )
        for p in rows
    ]


def _manual_entries(association, start, end) -> list[Entry]:
    out = []
    for item in ManualItem.all_associations.filter(association=association, active=True):
        for day in item.due_dates(start, end):
            out.append(
                Entry(
                    uid=f"compliance-item-{item.pk}-{day.isoformat()}.a{association.pk}@homeoa",
                    kind="manual",
                    title=item.title,
                    due=day,
                    url=reverse("compliance:item", args=[item.pk]),
                    detail=item.notes,
                )
            )
    return out


def _vendor_entries(association, start, end) -> list[Entry]:
    """Certificate expiries and contract ends from ``vendors`` (read-only here)."""
    from homeoa.vendors.alerts import current_documents
    from homeoa.vendors.models import Contract, Vendor

    out = []
    for doc in current_documents(association):
        if start <= doc.expiry_date <= end:
            out.append(
                Entry(
                    uid=f"compliance-vdoc-{doc.pk}.a{association.pk}@homeoa",
                    kind="coi",
                    title=_("%(kind)s expires: %(vendor)s")
                    % {"kind": doc.get_kind_display(), "vendor": doc.vendor.name},
                    due=doc.expiry_date,
                    url=reverse("vendors:detail", args=[doc.vendor.slug]),
                    read_only=True,
                )
            )
    contracts = Contract.all_associations.filter(
        association=association,
        vendor__status=Vendor.Status.ACTIVE,
        end_date__gte=start,
        end_date__lte=end,
    ).select_related("vendor")
    for c in contracts:
        out.append(
            Entry(
                uid=f"compliance-contract-{c.pk}.a{association.pk}@homeoa",
                kind="contract",
                title=_("Contract ends: %(title)s (%(vendor)s)")
                % {"title": c.title, "vendor": c.vendor.name},
                due=c.end_date,
                url=reverse("vendors:detail", args=[c.vendor.slug]),
                detail=_("Notice by %(date)s") % {"date": c.notice_date.isoformat()}
                if c.notice_days
                else "",
                read_only=True,
            )
        )
    return out


def _insurance_entries(association, start, end) -> list[Entry]:
    """The master policy's renewal, from the current term's end date (read-only here)."""
    from homeoa.insurance.services import renewal_due

    return [
        Entry(
            uid=f"compliance-insurance-{p.pk}.a{association.pk}@homeoa",
            kind="insurance",
            title=_("Master insurance policy renews: %(carrier)s") % {"carrier": p.carrier},
            due=p.end_date,
            url=reverse("insurance:index"),
            detail=_("Add the new term under Documents > Insurance."),
            read_only=True,
        )
        for p in renewal_due(association, start, end)
    ]


def _contributed_entries(association, start, end) -> list[Entry]:
    """Dates that optional modules add (for example preventive maintenance)."""
    from homeoa.plugins import slots

    return [
        e
        for fn in slots.values(slots.COMPLIANCE_CALENDAR_ENTRIES, association)
        for e in fn(association, start, end)
    ]


def entries(association, start: dt.date, end: dt.date, *, vendors: bool = True) -> list[Entry]:
    rows = (
        _period_entries(association, start, end)
        + _manual_entries(association, start, end)
        + _insurance_entries(association, start, end)
    )
    if vendors:
        rows += _vendor_entries(association, start, end) + _contributed_entries(
            association, start, end
        )
    return sorted(rows, key=lambda e: (e.due, e.title))


def overdue_items(association, today: dt.date) -> list[ManualItem]:
    return list(
        ManualItem.all_associations.filter(
            association=association, active=True, next_due__lt=today
        ).order_by("next_due", "pk")
    )


# ---------------------------------------------------------------- community source


def calendar_source(association, start: dt.date, end: dt.date):
    """Board-only rows for the community calendar and iCal feed."""
    from homeoa.community.models import Audience
    from homeoa.community.services import Occurrence

    return [
        Occurrence(
            uid=e.uid,
            title=_("Due: %(title)s") % {"title": e.title},
            start_date=e.due,
            description=e.detail,
            audience=Audience.BOARD,
            source=SOURCE_KEY,
            url=e.url,
        )
        for e in entries(association, start, end, vendors=False)
    ]


# ---------------------------------------------------------------- reminders


def reminder_offsets(association) -> tuple[int, ...]:
    """Days before a deadline to remind the board (association setting, default 30 and 7)."""
    raw = (getattr(association, "settings", None) or {}).get(OFFSETS_KEY)
    try:
        days = tuple(sorted({int(d) for d in raw if int(d) > 0}, reverse=True))
    except TypeError, ValueError:
        return DEFAULT_OFFSETS
    return days or DEFAULT_OFFSETS


def parse_offsets(text: str) -> tuple[int, ...]:
    try:
        days = {int(p) for p in text.replace(",", " ").split()}
    except ValueError:
        raise CalendarError(_("Enter whole numbers of days, e.g. 30, 7.")) from None
    if not days or any(d < 1 or d > 365 for d in days):
        raise CalendarError(_("Each reminder must be between 1 and 365 days before."))
    return tuple(sorted(days, reverse=True))


@transaction.atomic
def set_reminder_offsets(association, days, by=None) -> None:
    days = list(days)
    settings = dict(association.settings or {})
    old = settings.get(OFFSETS_KEY)
    settings[OFFSETS_KEY] = days
    association.settings = settings
    association.save(update_fields=["settings"])
    audit.record(
        "compliance.reminder_offsets", association, actor=by, changes={OFFSETS_KEY: [old, days]}
    )


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=REMINDER_TYPE,
            label=gettext_lazy("Compliance deadline reminder"),
            default_channels=("inapp", "email"),
            audience=ntypes.AUDIENCE_BOARD,
            subject_template=(
                "{% load i18n %}{% blocktranslate with t=title d=due %}"
                "Due {{ d }}: {{ t }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with t=title d=due n=days %}"
                "{{ t }} is due on {{ d }} ({{ n }} days from now).{% endblocktranslate %}"
                "\n\n{{ url }}\n\n{{ association_name }}"
            ),
        )
    )


def _offset_due(due: dt.date, as_of: dt.date, offsets) -> int | None:
    """The nearest offset whose reminder day has come for a future ``due``, if any."""
    if due < as_of:
        return None
    started = [d for d in offsets if due - dt.timedelta(days=d) <= as_of]
    return min(started) if started else None


def send_reminders(association, as_of: dt.date) -> int:
    """Queue reminders (after commit) for every open deadline whose reminder day has come.

    Once per item, due date and offset: ``notify``'s idempotency key carries all three. When
    the job missed days, only the nearest offset is sent. Returns the reminders queued."""
    from homeoa.notify.services import board_recipients, notify_after_commit

    offsets = reminder_offsets(association)
    horizon = as_of + dt.timedelta(days=max(offsets))
    sent = 0
    recipients = None
    for e in entries(association, as_of, horizon, vendors=False):
        if e.done:
            continue
        days = _offset_due(e.due, as_of, offsets)
        if days is None:
            continue
        if recipients is None:
            recipients = board_recipients(association)
        notify_after_commit(
            REMINDER_TYPE,
            recipients,
            {
                "title": e.title,
                "due": e.due.isoformat(),
                "days": (e.due - as_of).days,
                "url": e.url,
                "association_name": association.name,
            },
            as_of,
            obj=e.uid,
            period=f"{e.due.isoformat()}:{days}",
            association=association,
        )
        sent += 1
    return sent
