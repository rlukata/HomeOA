"""Demo governance data for seed_demo: board roster, a committee and resolutions.

Names are the fictional Cedar Row owners. The board logins (treasurer, secretary) are
linked to their owner Person by ``core.demo``. Resolutions are last year's, so a new
draft this year is numbered 2026-01.
"""

from __future__ import annotations

import datetime as dt

from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.documents.demo import pdf

from . import services
from .models import Committee, Office, Visibility

TERM_START = dt.date(2025, 1, 15)
TERM_END = dt.date(2026, 12, 31)

# (unit whose owner holds the office, office)
ROSTER = [
    ("I", Office.PRESIDENT),
    ("E", Office.VICE_PRESIDENT),
    ("D", Office.SECRETARY),
    ("B", Office.TREASURER),
]

# (name, purpose, [(unit whose owner sits on it, is chair)])
COMMITTEES = [
    (
        "Landscaping committee",
        "Plantings, irrigation and the grounds contract",
        [("C", True), ("H", False)],
    ),
    (
        "Maintenance committee",
        "Repairs and one-off jobs up to the board's limit",
        [("H", True), ("G", False)],
    ),
]

# (title, text, adopted on, PDF visible to)
RESOLUTIONS = [
    (
        "Collection policy",
        "Dues are due on the 10th; reminders and late notices follow the board's "
        "delinquency schedule.",
        dt.date(2025, 6, 12),
        Visibility.OWNERS,
    ),
    (
        "Gutter cleaning contract",
        "Engage a contractor for twice-yearly gutter cleaning.",
        dt.date(2025, 9, 18),
        Visibility.BOARD,
    ),
]


def seed(association, owners: dict) -> None:
    """``owners``: {unit number: owner Person}."""
    board = []
    for unit, office in ROSTER:
        person = owners[unit]
        services.add_term(association, person=person, office=office, start=TERM_START, end=TERM_END)
        board.append(person)

    for name, purpose, members in COMMITTEES:
        committee = Committee.objects.create(association=association, name=name, purpose=purpose)
        for unit, chair in members:
            services.add_committee_member(
                committee, person=owners[unit], start=TERM_START, is_chair=chair
            )

    for title, text, adopted_on, visibility in RESOLUTIONS:
        r = services.create_resolution(association, title=title, text=text, year=2025)
        services.record_votes(r, {p: "for" for p in board})
        adopted = f"{adopted_on:%B} {adopted_on.day}, {adopted_on.year}"
        content = pdf(
            f"Resolution {r.number}: {title}", [text, f"Adopted by the board on {adopted}."]
        )
        services.adopt(
            r,
            adopted_on=adopted_on,
            pdf=SimpleUploadedFile("resolution.pdf", content, content_type="application/pdf"),
            visibility=visibility,
        )
