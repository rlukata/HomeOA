"""The setup wizard's fine answers (``fines.*``): adopting them makes a new fine schedule."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.utils.translation import gettext as _

from homeoa.setup import targets

from .models import FineSchedule, FineScheduleLine, ViolationCategory
from .services import next_schedule_version


def apply_fines(association, answers: dict, *, resolution, on: dt.date, reason: str) -> None:
    """A new fine schedule version with the same amounts for every active category. With
    no categories yet, one general category is added (the board can split it later)."""
    fines = {k.split(".", 1)[1]: v for k, v in answers.items()}
    categories = list(
        ViolationCategory.all_associations.filter(association=association, active=True)
    )
    if not categories:
        categories = [
            ViolationCategory.all_associations.create(
                association=association,
                name=targets.GENERAL_CATEGORY,
                rule_citation="Rules and regulations of the association",
            )
        ]
    continuing = Decimal(fines.get("continuing_amount") or 0)
    days = int(fines.get("continuing_days") or 0)
    schedule = FineSchedule.all_associations.create(
        association=association,
        version=next_schedule_version(association),
        effective_on=on,
        adopted_on=on,
        resolution=resolution,
        note=_("Adopted with association setup."),
    )
    for category in categories:
        FineScheduleLine.all_associations.create(
            association=association,
            schedule=schedule,
            category=category,
            first_amount=Decimal(fines.get("first_amount") or 0),
            repeat_amount=Decimal(fines.get("repeat_amount") or 0),
            continuing_amount=continuing if continuing and days else None,
            continuing_period_days=days if continuing and days else None,
        )
