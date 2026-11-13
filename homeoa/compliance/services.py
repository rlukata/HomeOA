"""Compliance services: open periods, choose a path, answer exemptions, record
evidence, complete, and flag missed deadlines. Every write is audited (models are
registered with ``homeoa.audit``)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeoa import rules
from homeoa.rules.base import Frequency, Obligation

from .models import Evidence, ObligationPeriod, PeriodStatus


class ComplianceError(ValueError):
    """A request the period's state or the obligation does not allow."""


# ---------------------------------------------------------------- pack lookup


def obligations(association) -> dict[str, tuple[Obligation, ...]]:
    """Obligation versions by id from the association's active pack, oldest first."""
    out: dict[str, list[Obligation]] = {}
    for ob in rules.active_pack(association).obligations:
        out.setdefault(ob.id, []).append(ob)
    return {
        k: tuple(sorted(v, key=lambda o: o.effective_from or dt.date.min)) for k, v in out.items()
    }


def _fy(association) -> int:
    return getattr(association, "fiscal_year_start_month", 1) or 1


def version_for_year(versions, year: int, fiscal_start_month: int = 1) -> Obligation | None:
    """The version in force on the period's due date."""
    for ob in versions:
        if ob.in_force(ob.due_date(year, fiscal_start_month)):
            return ob
    return None


def obligation_for(period: ObligationPeriod) -> Obligation | None:
    versions = obligations(period.association).get(period.obligation_id, ())
    for ob in versions:
        if ob.in_force(period.due_date):
            return ob
    return None


@dataclass(frozen=True)
class PathView:
    id: str
    label: str
    help: str
    citation: str
    source_url: str
    threshold: rules.Resolved | None


def paths(period: ObligationPeriod) -> list[PathView]:
    """The obligation's paths with any threshold resolved on the due date."""
    ob = obligation_for(period)
    if ob is None:
        return []
    out = []
    for p in ob.paths:
        threshold = (
            rules.resolve(period.association, p.threshold_key, as_of=period.due_date)
            if p.threshold_key
            else None
        )
        out.append(PathView(p.id, str(p.label), str(p.help), p.citation, p.source_url, threshold))
    return out


# ---------------------------------------------------------------- job


def _create(association, ob: Obligation, year: int) -> ObligationPeriod:
    period, _created = ObligationPeriod.all_associations.get_or_create(
        association=association,
        obligation_id=ob.id,
        year=year,
        defaults={
            "title": str(ob.title)[:200],
            "citation": ob.citation,
            "opens_on": ob.opens_on(year, _fy(association)),
            "due_date": ob.due_date(year, _fy(association)),
        },
    )
    return period


def _next_year(association, ob_id: str, versions, as_of: dt.date) -> int | None:
    """For an every-N-years obligation: the next period's year, or None while one is open."""
    latest = (
        ObligationPeriod.all_associations.filter(association=association, obligation_id=ob_id)
        .order_by("-year")
        .first()
    )
    if latest is None:
        return as_of.year
    if latest.status == PeriodStatus.OPEN:
        return None
    if latest.status == PeriodStatus.EXEMPT:
        return latest.year + 1
    ob = version_for_year(versions, latest.year, _fy(association)) or versions[-1]
    return latest.year + ob.every_years


def run(association, as_of: dt.date) -> list[ObligationPeriod]:
    """Open every period whose opening date has come; flag open periods past due as missed.

    Starts from ``as_of``'s year: earlier years are not back-filled, so nothing needs to be
    answered up front. Returns the newly opened periods. Idempotent."""
    opened: list[ObligationPeriod] = []
    existing = set(
        ObligationPeriod.all_associations.filter(association=association).values_list(
            "obligation_id", "year"
        )
    )
    for ob_id, versions in obligations(association).items():
        if versions[0].frequency == Frequency.EVERY_N_YEARS:
            year = _next_year(association, ob_id, versions, as_of)
            years = [year] if year is not None else []
        else:
            years = [as_of.year, as_of.year + 1]
        for year in years:
            ob = version_for_year(versions, year, _fy(association))
            if (
                ob is None
                or (ob_id, year) in existing
                or ob.opens_on(year, _fy(association)) > as_of
            ):
                continue
            opened.append(_create(association, ob, year))
    for period in ObligationPeriod.all_associations.filter(
        association=association, status=PeriodStatus.OPEN, missed=False, due_date__lt=as_of
    ):
        period.missed = True
        period.save(update_fields=["missed"])
    return opened


# ---------------------------------------------------------------- board actions


def _require_open(period: ObligationPeriod) -> Obligation:
    if not period.is_open:
        raise ComplianceError(_("This period is already closed."))
    ob = obligation_for(period)
    if ob is None:
        raise ComplianceError(_("The rule pack no longer defines this obligation."))
    return ob


@transaction.atomic
def choose_path(period: ObligationPeriod, path_id: str, user=None) -> ObligationPeriod:
    ob = _require_open(period)
    if ob.path(path_id) is None:
        raise ComplianceError(_("Unknown path."))
    period.path_id = path_id
    period.path_chosen_at = timezone.now()
    period.path_chosen_by = user
    period.save(update_fields=["path_id", "path_chosen_at", "path_chosen_by"])
    return period


@transaction.atomic
def answer_exemption(
    period: ObligationPeriod, exemption_id: str, applies: bool, user=None, *, on=None
) -> ObligationPeriod:
    """Record a yes/no answer. "Yes" closes the period as exempt."""
    ob = _require_open(period)
    if ob.exemption(exemption_id) is None:
        raise ComplianceError(_("Unknown exemption."))
    answers = dict(period.exemption_answers or {})
    answers[exemption_id] = bool(applies)
    period.exemption_answers = answers
    fields = ["exemption_answers"]
    if applies:
        period.status = PeriodStatus.EXEMPT
        period.exemption_id = exemption_id
        period.closed_on = on or rules._today(period.association)
        period.closed_by = user
        fields += ["status", "exemption_id", "closed_on", "closed_by"]
    period.save(update_fields=fields)
    return period


@transaction.atomic
def add_evidence(
    period: ObligationPeriod,
    *,
    upload=None,
    url: str = "",
    note: str = "",
    user=None,
) -> Evidence:
    """Attach an uploaded file (a Django ``UploadedFile``) or a link to the period."""
    if not period.is_open:
        raise ComplianceError(_("This period is already closed."))
    url = (url or "").strip()
    if upload is None and not url:
        raise ComplianceError(_("Upload a document or give a link."))
    if url and not (url.startswith(("https://", "http://")) or url.startswith("/")):
        raise ComplianceError(_("A link must start with https:// or /."))
    ev = Evidence(association=period.association, period=period, url=url, note=note.strip())
    if upload is not None:
        from homeoa.providers import get_provider

        ctype = getattr(upload, "content_type", "") or "application/octet-stream"
        stored = get_provider("FileStorage", period.association).put(
            period.association.pk,
            upload.name,
            upload.read(),
            content_type=ctype,
            folder="compliance",
        )
        ev.file_key = stored.key
        ev.filename = upload.name[:255]
        ev.content_type = ctype[:100]
    ev.added_by = user
    ev.save()
    return ev


@transaction.atomic
def complete(period: ObligationPeriod, user=None, *, on=None) -> ObligationPeriod:
    """Close the period as done: needs a chosen path and at least one piece of evidence."""
    _require_open(period)
    if not period.path_id:
        raise ComplianceError(_("Choose which path the association took first."))
    if not period.evidence.exists():
        raise ComplianceError(_("Add the evidence first (a document or a link)."))
    period.status = PeriodStatus.DONE
    period.closed_on = on or rules._today(period.association)
    period.closed_by = user
    period.save(update_fields=["status", "closed_on", "closed_by"])
    return period


# ---------------------------------------------------------------- to-dos


def todo_items(association) -> list[dict]:
    """Board to-dos: choose a path, record evidence, or a missed deadline."""
    from django.urls import reverse

    items = []
    for period in ObligationPeriod.all_associations.filter(
        association=association, status=PeriodStatus.OPEN
    ).order_by("due_date", "pk"):
        url = reverse("compliance:period", args=[period.pk])
        name = f"{period.title} ({period.year})"
        if period.missed:
            title = _("Missed deadline: %(name)s") % {"name": name}
            kind = "enforce"
        elif not period.path_id:
            title = _("Choose how to meet: %(name)s") % {"name": name}
            kind = "gov"
        else:
            title = _("Record evidence: %(name)s") % {"name": name}
            kind = "gov"
        items.append(
            {
                "id": f"compliance-{period.pk}",
                "kind": kind,
                "icon": "alert" if period.missed else "users",
                "title": title,
                "sub": _("Due %(date)s · %(citation)s")
                % {"date": period.due_date.isoformat(), "citation": period.citation},
                "due": period.due_date,
                "url": url,
                "action": _("Open"),
            }
        )
    from .calendar import overdue_items

    for item in overdue_items(association, rules._today(association)):
        items.append(
            {
                "id": f"compliance-item-{item.pk}",
                "kind": "enforce",
                "icon": "alert",
                "title": _("Overdue: %(name)s") % {"name": item.title},
                "sub": _("Was due %(date)s") % {"date": item.next_due.isoformat()},
                "due": item.next_due,
                "url": reverse("compliance:item", args=[item.pk]),
                "action": _("Open"),
            }
        )
    return items
