"""The violations answer on a resale certificate."""

from __future__ import annotations

import datetime as dt

from django.utils.translation import gettext as _


def violations_answer(unit, on: dt.date) -> str:
    from homeoa.enforcement.models import Violation

    rows = Violation.all_associations.filter(
        association=unit.association, unit=unit, status=Violation.Status.OPEN
    ).select_related("category")
    if not rows:
        return _("None known.")
    return "\n".join(
        _("Open: %(category)s, observed %(date)s.")
        % {"category": v.category, "date": v.observed_on.isoformat()}
        for v in rows
    )
