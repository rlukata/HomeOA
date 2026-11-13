"""Community services: audiences, announcements, calendar sources and the iCal feed.

Audiences: everyone sees ``all``; owners add ``owners``, tenants add ``tenants``;
the board sees every audience (it manages them). Residents count only while the resident
portal is on (``access.services.roles_for`` already applies that gate).

Calendar sources: other apps (meetings, compliance) add events with
``register_source(key, label, fn)``; ``fn(association, start, end)`` returns
``Occurrence`` rows. Stored manual events are the built-in source.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.services import roles_for
from homeoa.core.context import association_today
from homeoa.notify import types as ntypes

from .models import Announcement, Audience, CalendarEvent, CalendarFeedToken

NOTIFY_TYPE = "community.announcement"


# --- audiences ------------------------------------------------------------------------


def audiences_for(user, association) -> frozenset[str]:
    """The audiences ``user`` may see in ``association`` (empty: none, not even ``all``)."""
    roles = roles_for(user, association)
    if perms.BOARD in roles:
        return frozenset(Audience.values)
    out: set[str] = set()
    if perms.OWNER in roles:
        out |= {Audience.ALL, Audience.OWNERS}
    if perms.TENANT in roles:
        out |= {Audience.ALL, Audience.TENANTS}
    if perms.COMMITTEE in roles:
        out.add(Audience.ALL)
    return frozenset(out)


def unit_audiences(is_owner: bool) -> frozenset[str]:
    """What an owner (or tenant) of a unit sees; used by the board's preview-as-unit."""
    return frozenset({Audience.ALL, Audience.OWNERS if is_owner else Audience.TENANTS})


# --- announcements --------------------------------------------------------------------


def visible_announcements(association, audiences: Iterable[str], today: dt.date | None = None):
    today = today or association_today(association)
    return (
        Announcement.all_associations.filter(
            association=association, audience__in=list(audiences), publish_on__lte=today
        )
        .exclude(expires_on__lt=today)
        .order_by("-pinned", "-publish_on", "-pk")
    )


def recipients(association, audience: str, on: dt.date) -> list:
    """``registry.Person`` rows for an audience on a day, deduplicated, in a stable order."""
    from homeoa.notify.services import board_recipients
    from homeoa.registry.models import Occupancy, Unit
    from homeoa.registry.services import current_occupants, current_owners

    people: dict[int, object] = {}
    units = Unit.all_associations.filter(association=association).order_by("pk")
    if audience in (Audience.ALL, Audience.OWNERS):
        for unit in units:
            for person in current_owners(unit, on):
                people.setdefault(person.pk, person)
    if audience in (Audience.ALL, Audience.TENANTS):
        for unit in units:
            for occ in current_occupants(unit, on, kind=Occupancy.Kind.TENANT):
                people.setdefault(occ.person.pk, occ.person)
    if audience in (Audience.ALL, Audience.BOARD):
        for person in board_recipients(association):
            people.setdefault(person.pk, person)
    return list(people.values())


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="announcements",
            key=NOTIFY_TYPE,
            label=gettext_lazy("Association announcement"),
            # Email only with e-notice consent (notify drops it otherwise); never by letter.
            default_channels=("inapp", "email"),
            subject_template=(
                "{% load i18n %}{% blocktranslate with a=association_name t=title %}"
                "{{ a }}: {{ t }}{% endblocktranslate %}"
            ),
            body_template="{{ body }}\n\n{{ association_name }}",
        )
    )


def send_announcement(announcement: Announcement, today: dt.date | None = None) -> bool:
    """Queue the "also notify" send once the caller's transaction commits.

    Only for a live announcement that asked for it and was not sent yet; returns whether
    a send was queued. ``notify``'s idempotency key (type, recipient, announcement) keeps
    a repeat from reaching anyone twice.
    """
    from homeoa.notify.services import notify_after_commit

    association = announcement.association
    today = today or association_today(association)
    if not announcement.also_notify or announcement.notified_on or not announcement.is_live(today):
        return False
    Announcement.all_associations.filter(pk=announcement.pk).update(notified_on=today)
    announcement.notified_on = today
    notify_after_commit(
        NOTIFY_TYPE,
        recipients(association, announcement.audience, today),
        {
            "title": announcement.title,
            "body": announcement.body,
            "association_name": association.name,
        },
        today,
        obj=announcement,
        association=association,
    )
    return True


@transaction.atomic
def save_announcement(announcement: Announcement, user=None) -> Announcement:
    if announcement.pk is None and user is not None:
        announcement.created_by = user
    announcement.full_clean(exclude=["association"])
    announcement.save()
    send_announcement(announcement)
    return announcement


def send_due(association, as_of: dt.date) -> int:
    """Send the "also notify" announcements whose publish day has come (daily job)."""
    due = Announcement.all_associations.filter(
        association=association, also_notify=True, notified_on__isnull=True, publish_on__lte=as_of
    ).exclude(expires_on__lt=as_of)
    return sum(1 for a in due if send_announcement(a, as_of))


# --- calendar -------------------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    """One calendar row, stored or from a registered source. ``uid`` must be stable."""

    uid: str
    title: str
    start_date: dt.date
    start_time: dt.time | None = None
    end_date: dt.date | None = None
    end_time: dt.time | None = None
    location: str = ""
    description: str = ""
    audience: str = Audience.ALL
    source: str = CalendarEvent.MANUAL
    stamp: dt.datetime | None = None
    url: str = ""
    event_id: int | None = None  # set for stored (editable) events

    @property
    def all_day(self) -> bool:
        return self.start_time is None


SourceFn = Callable[[object, dt.date, dt.date], Iterable[Occurrence]]


@dataclass(frozen=True)
class Source:
    key: str
    label: str
    fn: SourceFn


_sources: dict[str, Source] = {}


def register_source(key: str, label, fn: SourceFn) -> Source:
    """Add a calendar source (e.g. meetings, compliance deadlines). Re-registering a key
    replaces it."""
    if key == CalendarEvent.MANUAL:
        raise ValueError("'manual' is the built-in source")
    source = Source(key, label, fn)
    _sources[key] = source
    return source


def unregister_source(key: str) -> None:
    _sources.pop(key, None)


def sources() -> list[Source]:
    return sorted(_sources.values(), key=lambda s: s.key)


def event_uid(event: CalendarEvent) -> str:
    return f"event-{event.pk}.a{event.association_id}@homeoa"


def _from_event(event: CalendarEvent) -> Occurrence:
    return Occurrence(
        uid=event_uid(event),
        title=event.title,
        start_date=event.start_date,
        start_time=event.start_time,
        end_date=event.end_date,
        end_time=event.end_time,
        location=event.location,
        description=event.description,
        audience=event.audience,
        source=event.source,
        stamp=event.updated_at,
        event_id=event.pk,
    )


def occurrences(
    association, audiences: Iterable[str], start: dt.date, end: dt.date
) -> list[Occurrence]:
    """Events overlapping ``start``..``end`` (inclusive) that ``audiences`` may see."""
    audiences = set(audiences)
    if not audiences:
        return []
    stored = (
        CalendarEvent.all_associations.filter(
            association=association, audience__in=audiences, start_date__lte=end
        )
        .exclude(end_date__lt=start)
        .exclude(end_date__isnull=True, start_date__lt=start)
    )
    rows = [_from_event(e) for e in stored]
    for source in sources():
        for occ in source.fn(association, start, end):
            if occ.audience in audiences:
                rows.append(occ)
    return sorted(rows, key=lambda o: (o.start_date, o.start_time or dt.time.min, o.title))


def upcoming(association, audiences, today: dt.date | None = None, days: int = 90, limit=None):
    today = today or association_today(association)
    rows = occurrences(association, audiences, today, today + dt.timedelta(days=days))
    return rows[:limit] if limit else rows


# --- iCal feed -------------------------------------------------------------------------

FEED_PAST_DAYS = 365
FEED_FUTURE_DAYS = 730


def feed_token(user, association, *, create: bool = True) -> CalendarFeedToken | None:
    live = CalendarFeedToken.all_associations.filter(
        association=association, user=user, revoked_at__isnull=True
    ).first()
    if live is None and create:
        live = CalendarFeedToken.all_associations.create(association=association, user=user)
    return live


def revoke_token(user, association) -> None:
    CalendarFeedToken.all_associations.filter(
        association=association, user=user, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())


def reset_token(user, association) -> CalendarFeedToken:
    with transaction.atomic():
        revoke_token(user, association)
        return feed_token(user, association)


def token_owner(token: str) -> CalendarFeedToken | None:
    return (
        CalendarFeedToken.all_associations.filter(token=token, revoked_at__isnull=True)
        .select_related("user", "association")
        .first()
    )


def _tz(association):
    with suppress(ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(association.timezone)
    return ZoneInfo("UTC")


def build_ics(association, rows: Iterable[Occurrence]) -> bytes:
    """An RFC 5545 VCALENDAR with a VTIMEZONE for the association's zone."""
    from icalendar import Calendar, Event

    tz = _tz(association)
    cal = Calendar()
    cal.add("prodid", "-//HomeOA//Community calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", association.name)
    cal.add("x-wr-timezone", tz.key)
    fallback_stamp = timezone.now()
    for occ in rows:
        ev = Event()
        ev.add("uid", occ.uid)
        ev.add("dtstamp", (occ.stamp or fallback_stamp).astimezone(dt.UTC))
        ev.add("summary", occ.title)
        if occ.all_day:
            ev.add("dtstart", occ.start_date)
            # DTEND of an all-day event is exclusive.
            ev.add("dtend", (occ.end_date or occ.start_date) + dt.timedelta(days=1))
        else:
            ev.add("dtstart", dt.datetime.combine(occ.start_date, occ.start_time, tzinfo=tz))
            if occ.end_time is not None:
                end_day = occ.end_date or occ.start_date
                ev.add("dtend", dt.datetime.combine(end_day, occ.end_time, tzinfo=tz))
        if occ.location:
            ev.add("location", occ.location)
        if occ.description:
            ev.add("description", occ.description)
        if occ.url:
            ev.add("url", occ.url)
        cal.add_component(ev)
    cal.add_missing_timezones()
    return cal.to_ical()


def feed_for(token: CalendarFeedToken, today: dt.date | None = None) -> bytes | None:
    """The user's feed, or None when they may see nothing (e.g. residents are off)."""
    association = token.association
    audiences = audiences_for(token.user, association) if token.user.is_active else frozenset()
    if not audiences:
        return None
    today = today or association_today(association)
    rows = occurrences(
        association,
        audiences,
        today - dt.timedelta(days=FEED_PAST_DAYS),
        today + dt.timedelta(days=FEED_FUTURE_DAYS),
    )
    return build_ics(association, rows)
