"""Demo components and preventive tasks for seed_demo (made-up ages and costs)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .models import Component, PreventiveTask

D = dt.date
COMPONENTS = (
    ("Roof", Component.Category.ROOF, "All buildings", D(2008, 6, 1), 25, "fair", "185000.00"),
    (
        "Gutters & downspouts",
        Component.Category.GUTTERS,
        "All buildings",
        D(2015, 5, 1),
        20,
        "good",
        "18000.00",
    ),
    (
        "Exterior paint",
        Component.Category.PAINT,
        "All buildings",
        D(2017, 8, 1),
        10,
        "fair",
        "62000.00",
    ),
    (
        "Parking lot asphalt",
        Component.Category.PAVING,
        "Parking lot",
        D(2005, 7, 1),
        25,
        "poor",
        "48000.00",
    ),
    (
        "Wood fence",
        Component.Category.FENCING,
        "North property line",
        D(2012, 4, 1),
        15,
        "fair",
        "9500.00",
    ),
)


def _save(obj):
    obj.full_clean()
    obj.save()
    return obj


def seed(association) -> None:
    from homeoa.vendors.models import Vendor

    by_name = {}
    for name, cat, where, installed, life, cond, cost in COMPONENTS:
        by_name[name] = _save(
            Component(
                association=association,
                name=name,
                category=cat,
                location=where,
                installed_on=installed,
                useful_life_years=life,
                condition=cond,
                replacement_cost=Decimal(cost),
                cost_as_of=D(2026, 1, 15),
            )
        )
    gutters = Vendor.all_associations.filter(
        association=association, name="Evergreen Gutter & Roof"
    ).first()
    for title, comp, vendor, months, due, cost in (
        (
            "Clean gutters and downspouts",
            "Gutters & downspouts",
            gutters,
            6,
            D(2026, 10, 15),
            "650.00",
        ),
        ("Roof moss treatment and inspection", "Roof", gutters, 12, D(2027, 4, 1), "1200.00"),
        ("Walk the fence line for loose boards", "Wood fence", None, 12, D(2026, 10, 20), None),
    ):
        _save(
            PreventiveTask(
                association=association,
                title=title,
                component=by_name[comp],
                vendor=vendor,
                every_months=months,
                next_due=due,
                estimated_cost=Decimal(cost) if cost else None,
            )
        )
