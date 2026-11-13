"""Who is signed in, as registry records: their ``Person`` and their ownership periods.

Money pages show only the signed-in owner's own ownership periods: a buyer never
sees the seller's lines, and a past owner keeps seeing their own.
"""

from __future__ import annotations

from homeoa.registry.models import OwnershipPeriod, Person


def my_person(request) -> Person | None:
    return Person.objects.filter(association=request.association, user=request.user).first()


def my_periods(request) -> list[OwnershipPeriod]:
    person = my_person(request)
    if person is None:
        return []
    return list(
        OwnershipPeriod.objects.filter(shares__person=person)
        .select_related("unit")
        .order_by("-start", "-pk")
        .distinct()
    )
