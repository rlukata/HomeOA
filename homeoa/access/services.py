"""Access control service API (PLT-03).

* ``permissions_for(user, association=None)``: frozenset of permission codes held today.
* ``has_perm(user, code, obj=None, association=None)``: one check. ``.own`` codes with an
  ``obj`` pass only when the object's unit is one of ``units_for(user)``.
* ``has_any(user, codes, ...)``.
* ``roles_for(user, association=None)``: active role slugs (explicit plus derived).
* ``units_for(user, association=None)``: units the user owns or occupies today (via the
  ``registry.Person`` linked to the user). Computed even while residents are disabled.
* ``is_board_member(user, association)``: the MFA policy hook.
* ``finance_managers(association)``: who holds ``finance.manage`` today (the Treasurer).
* ``has_association_perm(user, code, ...)``: ``has_perm`` without the instance-admin grant.
* ``ensure_default_roles(association)`` / ``assign_role(...)`` / ``update_assignment(...)`` /
  ``end_assignment(...)``.

"Today" is the association's local date. Owner and tenant roles are derived from registry
data and grant nothing until the association is live and resident access is not paused
(``homeoa.core.status``).
"""

from __future__ import annotations

import contextlib
import datetime
from contextvars import ContextVar

from django.db import transaction

from homeoa.core import residents_enabled as _core_residents_enabled
from homeoa.core.context import get_current_association

from . import permissions as perms
from .models import Role, RoleAssignment

_CACHE_ATTR = "_homeoa_access_cache"

# (user pk, codes): extra codes one user holds for the duration of ``elevated(...)``.
_ELEVATED: ContextVar[tuple[int, frozenset[str]] | None] = ContextVar(
    "homeoa_access_elevated", default=None
)


@contextlib.contextmanager
def elevated(user, codes):
    """Let ``user`` hold ``codes`` inside this block only.

    Used to run an approved change request as the person who proposed it, and to check a
    proposal by running it once in a rolled-back transaction. Never used for page access.
    """
    token = _ELEVATED.set((user.pk, frozenset(codes)))
    try:
        yield
    finally:
        _ELEVATED.reset(token)


def _extra_codes(user) -> frozenset[str]:
    current = _ELEVATED.get()
    if current is None or user is None or current[0] != getattr(user, "pk", None):
        return frozenset()
    return current[1]


def today_for(association) -> datetime.date:
    from homeoa.registry.services import today_for as _today

    return _today(association)


def residents_enabled(association) -> bool:
    """Resident-access switch; delegates to ``homeoa.core.residents_enabled`` (single source)."""
    return _core_residents_enabled(association)


def ensure_default_roles(association) -> None:
    for slug, spec in perms.DEFAULT_ROLES.items():
        Role.all_associations.get_or_create(
            association=association,
            slug=slug,
            defaults={"name": str(spec["name"]), "is_system": True},
        )


def _association(association):
    return association if association is not None else get_current_association()


def _person(user, association):
    from homeoa.registry.models import Person

    return Person.all_associations.filter(association=association, user=user).first()


def _resident_roles(user, association, day) -> tuple[set[str], list]:
    from homeoa.registry.models import Occupancy
    from homeoa.registry.services import units_owned_by

    person = _person(user, association)
    if person is None:
        return set(), []
    roles: set[str] = set()
    units = list(units_owned_by(person, on=day))
    if units:
        roles.add(perms.OWNER)
    occupied = (
        Occupancy.all_associations.filter(person=person, start__lte=day)
        .exclude(end__lt=day)
        .select_related("unit")
    )
    for occ in occupied:
        if occ.kind == Occupancy.Kind.TENANT:
            roles.add(perms.TENANT)
        if occ.unit not in units:
            units.append(occ.unit)
    return roles, units


def _compute(user, association):
    day = today_for(association)
    cache = getattr(user, _CACHE_ATTR, None)
    key = (association.pk, day)
    if cache and cache[0] == key:
        return cache[1]
    slugs: set[str] = set()
    codes: set[str] = set()
    assignments = RoleAssignment.all_associations.filter(
        association=association, user=user, start__lte=day
    ).exclude(end__lt=day)
    for a in assignments.select_related("role"):
        slugs.add(a.role.slug)
        codes |= a.role.permission_set
        if a.role.slug == perms.BOARD and a.title:
            codes |= perms.TITLE_PERMISSIONS.get(a.title, frozenset())
    resident_slugs, units = _resident_roles(user, association, day)
    if residents_enabled(association):
        for slug in resident_slugs:
            slugs.add(slug)
            codes |= perms.DEFAULT_ROLES[slug]["permissions"]
    association_codes = frozenset(codes)
    if user.is_superuser:
        slugs.add(perms.SYS_ADMIN)
        codes |= perms.DEFAULT_ROLES[perms.SYS_ADMIN]["permissions"]
    result = {
        "roles": frozenset(slugs),
        "codes": frozenset(codes),
        "association_codes": association_codes,
        "units": units,
    }
    setattr(user, _CACHE_ATTR, (key, result))
    return result


def clear_cache(user) -> None:
    if hasattr(user, _CACHE_ATTR):
        delattr(user, _CACHE_ATTR)


_EMPTY = {
    "roles": frozenset(),
    "codes": frozenset(),
    "association_codes": frozenset(),
    "units": [],
}


def _data(user, association):
    association = _association(association)
    if association is None or user is None or not getattr(user, "is_authenticated", False):
        return _EMPTY
    if not user.is_active:
        return _EMPTY
    return _compute(user, association)


def permissions_for(user, association=None) -> frozenset[str]:
    return _data(user, association)["codes"] | _extra_codes(user)


def roles_for(user, association=None) -> frozenset[str]:
    return _data(user, association)["roles"]


def units_for(user, association=None) -> list:
    return list(_data(user, association)["units"])


def _unit_of(obj):
    from homeoa.registry.models import Unit

    if isinstance(obj, Unit):
        return obj
    return getattr(obj, "unit", None)


def has_perm(user, code: str, obj=None, association=None) -> bool:
    data = _data(user, association)
    if code not in data["codes"] and code not in _extra_codes(user):
        return False
    if code.endswith(".own") and obj is not None:
        unit = _unit_of(obj)
        return unit is not None and unit in data["units"]
    return True


def has_any(user, codes, obj=None, association=None) -> bool:
    return any(has_perm(user, c, obj, association) for c in codes)


def has_association_perm(user, code: str, association=None) -> bool:
    """Like ``has_perm`` but ignoring what being a superuser alone grants.

    A superuser sees no association data by default: e.g. the audit log shows them only
    events with no association unless they also hold an association role (System admin
    assigned in the association counts).
    """
    return code in _data(user, association)["association_codes"] or code in _extra_codes(user)


def is_board_member(user, association) -> bool:
    if association is None:
        return False
    return perms.BOARD in _data(user, association)["roles"]


@transaction.atomic
def assign_role(
    user, association, role: Role | str, *, start=None, end=None, title="", unit=None
) -> RoleAssignment:
    if isinstance(role, str):
        ensure_default_roles(association)
        role = Role.all_associations.get(association=association, slug=role)
    if not role.assignable:
        raise ValueError(f"Role {role.slug!r} is derived and cannot be assigned.")
    assignment = RoleAssignment(
        association=association,
        user=user,
        role=role,
        start=start or today_for(association),
        end=end,
        title=title,
        unit=unit,
    )
    assignment.full_clean()
    assignment.save()
    clear_cache(user)
    return assignment


def update_assignment(assignment: RoleAssignment, *, title, start, end) -> RoleAssignment:
    """Change an assignment's officer title and dates in place. The audit log records the
    before and after values, so past terms stay on record without separate rows."""
    assignment.title, assignment.start, assignment.end = title, start, end
    assignment.full_clean()
    assignment.save()
    clear_cache(assignment.user)
    return assignment


def end_assignment(assignment: RoleAssignment, end=None) -> RoleAssignment | None:
    """End an assignment (default: yesterday, so access stops today).

    An assignment that would end before it starts is deleted instead (still audited).
    """
    day = end or today_for(assignment.association) - datetime.timedelta(days=1)
    user = assignment.user
    if day < assignment.start:
        assignment.delete()
        clear_cache(user)
        return None
    assignment.end = day
    assignment.save()
    clear_cache(user)
    return assignment


def finance_managers(association) -> dict:
    """Money access today: ``{"treasurers": [...]}`` active Treasurer assignments."""
    day = today_for(association)
    active = (
        RoleAssignment.all_associations.filter(
            association=association,
            role__slug=perms.BOARD,
            title=RoleAssignment.Title.TREASURER,
            start__lte=day,
        )
        .exclude(end__lt=day)
        .select_related("user", "role")
    )
    return {"treasurers": list(active)}


def board_members(association, day=None) -> list:
    """Users with an active Board role on ``day`` (default today)."""
    from django.contrib.auth import get_user_model

    day = day or today_for(association)
    ids = (
        RoleAssignment.all_associations.filter(
            association=association, role__slug=perms.BOARD, start__lte=day
        )
        .exclude(end__lt=day)
        .values_list("user_id", flat=True)
    )
    return list(get_user_model().objects.filter(pk__in=set(ids), is_active=True).order_by("email"))


def role_holders(association, slug: str, day=None) -> list:
    """Users holding the role ``slug`` on ``day`` (default today)."""
    from django.contrib.auth import get_user_model

    day = day or today_for(association)
    ids = (
        RoleAssignment.all_associations.filter(
            association=association, role__slug=slug, start__lte=day
        )
        .exclude(end__lt=day)
        .values_list("user_id", flat=True)
    )
    return list(get_user_model().objects.filter(pk__in=set(ids), is_active=True).order_by("email"))


# --- resident-access gate -----------------------------------------------------


def _has_explicit_role(user, association) -> bool:
    day = today_for(association)
    return (
        RoleAssignment.all_associations.filter(association=association, user=user, start__lte=day)
        .exclude(end__lt=day)
        .exists()
    )


def is_read_only(user, association) -> bool:
    """True when every hand-assigned role ``user`` holds in ``association`` is read-only
    (``permissions.READ_ONLY_ROLES``, the CPA). Superusers are never read-only."""
    if association is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return False
    explicit = _data(user, association)["roles"] - set(perms.DERIVED_ROLES)
    return bool(explicit) and explicit <= perms.READ_ONLY_ROLES


def is_resident_locked(user, association) -> bool:
    """True when ``user`` is a resident (current or past) with no board-side role while
    residents are switched off: they may see only the "not open yet" page."""
    from homeoa.registry.models import Person

    if association is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or residents_enabled(association):
        return False
    if not Person.all_associations.filter(association=association, user=user).exists():
        return False
    return not _has_explicit_role(user, association)


def may_request_login(user) -> bool:
    """Whether a sign-in link may be emailed to ``user``.

    Refused only for users who are resident-locked in every association they belong to.
    The login page answers the same either way, so this never reveals an account exists.
    """
    from homeoa.core.models import Association
    from homeoa.registry.models import Person

    if user.is_superuser:
        return True
    ids = set(Person.all_associations.filter(user=user).values_list("association_id", flat=True))
    if not ids:
        return True
    return any(not is_resident_locked(user, a) for a in Association.objects.filter(pk__in=ids))
