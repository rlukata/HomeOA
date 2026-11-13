"""Resident directory (REG-09): the owner records export, on screen.

Owners (not tenants) and the board see every unit's owners and residents with the
same redaction as the owner-mode records export (:func:`export.redact`): unlisted phones,
confidential emails, residents' emails and Address Confidentiality Program addresses are
left out.
"""

from __future__ import annotations

import datetime as dt

from . import export


def units(association, on: dt.date) -> list[dict]:
    """``[{"unit": Unit, "people": [...]}]`` in unit order; units nobody is listed on are left out.

    A person's address shows only when it differs from the unit's own address (an owner
    living elsewhere, or an ACP substitute address).
    """
    groups: dict[int, dict] = {}
    for e in export.roster(association, on):
        contact = export.redact(e.person, e.unit, is_owner=e.is_owner)
        unit_address = ", ".join(e.unit.address_lines)
        group = groups.setdefault(e.unit.pk, {"unit": e.unit, "people": []})
        group["people"].append(
            {
                "name": e.person.full_name,
                "role": e.role,
                "email": contact.email,
                "phone": contact.phone,
                "address": "" if contact.address == unit_address else contact.address,
            }
        )
    return list(groups.values())
