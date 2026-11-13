"""Owner records export (REG-05, RPT-01): the unit/owner roster with redaction.

Every roster export goes through :func:`roster_spec`, and every row through
:func:`redact`, so CSV, XLSX and PDF (one :class:`TableSpec`) cannot skip it.

Owner mode (the default, safe to hand to an owner or a successor manager) leaves out:

* unlisted phone numbers (``Person.phone_unlisted``);
* emails of owners who chose to keep them confidential (``Person.email_confidential``);
* every resident's (non-owner's) email;
* Address Confidentiality Program addresses, replaced by the ACP substitute address or
  left blank.

Full mode (board only, ``units.manage``, reason required, audited) keeps everything
and is watermarked. Personnel or medical records and unit files are never part of this
export: it reads only the registry roster.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.utils.translation import gettext as _

from homeoa.reports.export import NUMBER, Column, TableSpec

from . import services
from .models import Occupancy, OwnershipPeriod, Person, Unit

OWNER = "owner"
FULL = "full"
MODES = (OWNER, FULL)


@dataclass
class Contact:
    """The shareable contact fields of one roster row."""

    address: str
    email: str
    phone: str


def _address(person: Person, unit: Unit) -> str:
    lines = person.address_lines or unit.address_lines
    return ", ".join(lines)


def redact(person: Person, unit: Unit, *, is_owner: bool, mode: str = OWNER) -> Contact:
    """The contact a roster row may show for ``person`` in ``mode``. The one redaction point."""
    if mode not in MODES:
        raise ValueError(f"unknown records export mode {mode!r}")
    address = _address(person, unit)
    email = person.email
    phone = person.phone
    if mode == FULL:
        return Contact(address=address, email=email, phone=phone)
    if person.phone_unlisted:
        phone = ""
    if not is_owner or person.email_confidential:
        email = ""
    if person.acp_participant:
        # RCW 64.34.372(6): "redact or otherwise remove the address". The substitute
        # address is left out too, since printing it reveals the participation.
        address = str(_("Address withheld"))
    return Contact(address=address, email=email, phone=phone)


@dataclass
class RosterEntry:
    """One person on the roster: an owner of the unit, or someone living there."""

    unit: Unit
    person: Person
    role: str
    is_owner: bool
    percent: object = None


def roster(association, on: dt.date) -> list[RosterEntry]:
    """Owners, then other occupants, unit by unit. Shared by the export and the directory."""
    entries = []
    units = sorted(Unit.objects.filter(association=association), key=lambda u: u.sort_key)
    for unit in units:
        period = services.ownership_period_on(unit, on)
        shares = (
            list(period.shares.select_related("person").order_by("-is_primary", "pk"))
            if isinstance(period, OwnershipPeriod)
            else []
        )
        owner_ids = {s.person_id for s in shares}
        for share in shares:
            entries.append(RosterEntry(unit, share.person, _("Owner"), True, share.percent))
        for occ in services.current_occupants(unit, on):
            if occ.person_id in owner_ids:
                continue
            entries.append(
                RosterEntry(unit, occ.person, str(Occupancy.Kind(occ.kind).label), False)
            )
    return entries


def _rows(association, on: dt.date, mode: str) -> list[list]:
    rows = []
    for e in roster(association, on):
        c = redact(e.person, e.unit, is_owner=e.is_owner, mode=mode)
        rows.append(
            [
                e.unit.number,
                e.unit.allocated_interest,
                e.role,
                e.person.full_name,
                e.percent,
                c.address,
                c.email,
                c.phone,
            ]
        )
    return rows


def roster_spec(association, *, mode: str = OWNER, on: dt.date | None = None) -> TableSpec:
    """The roster table for every format. Redaction is applied row by row in ``redact``."""
    if mode not in MODES:
        raise ValueError(f"unknown records export mode {mode!r}")
    on = on or services.today_for(association)
    columns = [
        Column(_("Unit")),
        Column(_("Allocated interest (%)"), NUMBER),
        Column(_("Role")),
        Column(_("Name")),
        Column(_("Ownership share (%)"), NUMBER),
        Column(_("Mailing address")),
        Column(_("Email")),
        Column(_("Phone")),
    ]
    if mode == FULL:
        watermark = _("CONTAINS CONFIDENTIAL DATA: board use only")
        return TableSpec(
            title=_("Owner records (full, board only)"),
            columns=columns,
            rows=_rows(association, on, mode),
            subtitle=_("As of %(date)s") % {"date": on.isoformat()} + " · " + watermark,
            filename="owner-records-full-confidential",
            pii=True,
            watermark=watermark,
        )
    return TableSpec(
        title=_("Owner records"),
        columns=columns,
        rows=_rows(association, on, mode),
        subtitle=_(
            "As of %(date)s · Unlisted phones, confidential emails, resident emails "
            "and confidential addresses are left out."
        )
        % {"date": on.isoformat()},
        filename="owner-records",
        pii=True,
    )
