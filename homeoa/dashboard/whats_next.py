"""The Home "what's next" card: the setup steps of a new association, in order.

Each step is ticked from facts already in the database; nothing about the card is stored.
The card shows while any step is undone, with the first undone step marked as next.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.urls import reverse
from django.utils.translation import gettext as _


@dataclass
class Step:
    key: str
    label: str
    done: bool
    url: str = ""
    note: str = ""
    next: bool = False


def _board_added(association, today) -> bool:
    from homeoa.governance import services as gov

    return bool(gov.roster(association, today))


def _units_and_owners(association, today) -> bool:
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_owners

    units = list(Unit.all_associations.filter(association=association))
    return bool(units) and all(current_owners(u, today) for u in units)


def _dues_set_up(association) -> bool:
    from homeoa.billing.models import AssessmentSchedule

    return AssessmentSchedule.all_associations.filter(association=association, active=True).exists()


def _imports(association) -> tuple[bool, bool]:
    """``(any import run, any committed run)``."""
    from homeoa.imports.models import ImportRun

    runs = ImportRun.all_associations.filter(association=association)
    if not runs.exists():
        return False, False
    return True, runs.filter(status=ImportRun.Status.COMMITTED).exists()


def owners_invited(association, today) -> bool:
    """Every current owner with an email has a sent invitation or an account."""
    from homeoa.access.models import Invitation
    from homeoa.registry.models import Unit
    from homeoa.registry.services import current_owners

    waiting = set()
    for unit in Unit.all_associations.filter(association=association):
        for p in current_owners(unit, today):
            if p.email and not p.user_id:
                waiting.add(p.pk)
    if not waiting:
        return True
    invited = set(
        Invitation.all_associations.filter(
            association=association, person_id__in=waiting, status=Invitation.Status.SENT
        ).values_list("person_id", flat=True)
    )
    return waiting <= invited


def steps(association, user, today: dt.date) -> list[Step]:
    """The setup steps in order, the first undone one marked ``next``."""
    from homeoa.access import permissions as perms
    from homeoa.access.services import has_perm
    from homeoa.core import status
    from homeoa.setup import services as setup

    features = setup.section(association, setup.FEATURES)
    others = [st for st in setup.states(association) if st.section.key != setup.FEATURES]
    any_import, imported = _imports(association)
    can_go_live = has_perm(user, perms.SETTINGS_MANAGE, association=association)

    out = [
        Step(
            "board",
            _("Board added"),
            _board_added(association, today),
            reverse("governance:index"),
        ),
        Step(
            "features",
            _("Features picked"),
            features is not None and setup.state(association, features).done,
            reverse("setup:section", args=[setup.FEATURES]),
        ),
        Step(
            "policies",
            _("Policies adopted"),
            all(st.done for st in others),
            reverse("setup:index"),
        ),
        Step(
            "units",
            _("Units and owners entered"),
            _units_and_owners(association, today),
            reverse("registry:index"),
        ),
        Step("dues", _("Dues set up"), _dues_set_up(association), reverse("billing:index")),
    ]
    if any_import:
        out.append(Step("records", _("Records loaded"), imported, reverse("imports:index")))
    out.append(
        Step(
            "go_live",
            _("Go live"),
            status.is_live(association, today),
            reverse("access:go_live") if can_go_live else "",
            "" if can_go_live else _("The System admin goes live."),
        )
    )
    out.append(
        Step(
            "invite",
            _("Owners invited"),
            owners_invited(association, today),
            reverse("registry:index"),
            _("Invite each owner from their unit's page."),
        )
    )
    first = next((s for s in out if not s.done), None)
    if first is not None:
        first.next = True
    return out


def pending_steps(association, user, today: dt.date) -> list[Step]:
    """The steps while any is undone; empty once every step is done."""
    if association is None:
        return []
    found = steps(association, user, today)
    return found if any(not s.done for s in found) else []
