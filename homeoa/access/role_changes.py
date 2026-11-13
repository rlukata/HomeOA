"""Role changes go through change requests: assigning, editing and ending a role.

The person whose role changes may not approve it.

The view checks the change first (so mistakes show on the form at once), then proposes it;
it takes effect when the board approves (or at once while the association has no board).
Payloads hold plain values: ids, the email and ISO dates.
"""

from __future__ import annotations

import datetime as dt

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.changes import kinds
from homeoa.changes import services as changes

from . import permissions as perms
from . import services
from .models import Role, RoleAssignment

ASSIGN, EDIT, END = "access.assign", "access.edit", "access.end"


def _date(value) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _title(value) -> str:
    return str(dict(RoleAssignment.Title.choices).get(value or "", "")) or "—"


def _role_details(role: Role) -> list:
    labels = sorted(str(perms.LABELS[c]) for c in role.permission_set)
    return [(_("What the role allows"), "; ".join(labels) or "—")]


def check_assign(association, *, email, role, title, start, end) -> None:
    """Raise ``ValidationError`` if the assignment could not be made as asked."""
    user = get_user_model().objects.filter(email__iexact=email).first()
    candidate = RoleAssignment(
        association=association, user=user, role=role, title=title, start=start, end=end
    )
    candidate.full_clean(exclude=["user"] if user is None else None)


def propose_assign(association, *, user, email, role, title, start, end, reason=""):
    check_assign(association, email=email, role=role, title=title, start=start, end=end)
    details = [
        (_("Person"), email),
        (_("Role"), str(role)),
        (_("Officer title"), _title(title)),
        (_("From"), start.isoformat()),
        (_("To"), _iso(end) or "—"),
        *_role_details(role),
    ]
    summary = _("Give %(email)s the %(role)s role") % {"email": email, "role": role}
    if title:
        summary = _("Make %(email)s %(title)s") % {"email": email, "title": _title(title)}
    payload = {
        "email": email,
        "role": role.pk,
        "title": title or "",
        "start": _iso(start),
        "end": _iso(end),
    }
    subject = get_user_model().objects.filter(email__iexact=email).first()
    return changes.propose(
        association,
        ASSIGN,
        payload,
        user=user,
        summary=summary,
        details=details,
        reason=reason,
        excluded=[subject],
    )


def propose_edit(association, assignment, *, user, title, start, end, reason=""):
    candidate = RoleAssignment.all_associations.get(pk=assignment.pk)
    candidate.title, candidate.start, candidate.end = title, start, end
    candidate.full_clean()
    before = (assignment.title, assignment.start, assignment.end)
    details = [
        (_("Person"), assignment.user.email),
        (_("Role"), str(assignment.role)),
        (_("Officer title"), f"{_title(before[0])} → {_title(title)}"),
        (_("From"), f"{_iso(before[1])} → {_iso(start)}"),
        (_("To"), f"{_iso(before[2]) or '—'} → {_iso(end) or '—'}"),
    ]
    summary = _("Change %(email)s's %(role)s role") % {
        "email": assignment.user.email,
        "role": assignment.role,
    }
    payload = {
        "assignment": assignment.pk,
        "title": title or "",
        "start": _iso(start),
        "end": _iso(end),
    }
    return changes.propose(
        association,
        EDIT,
        payload,
        user=user,
        summary=summary,
        details=details,
        reason=reason,
        excluded=[assignment.user],
    )


def propose_end(association, assignment, *, user, reason=""):
    details = [
        (_("Person"), assignment.user.email),
        (_("Role"), str(assignment.role)),
        (_("Officer title"), _title(assignment.title)),
    ]
    summary = _("End %(email)s's %(role)s role") % {
        "email": assignment.user.email,
        "role": assignment.role,
    }
    return changes.propose(
        association,
        END,
        {"assignment": assignment.pk},
        user=user,
        summary=summary,
        details=details,
        reason=reason,
        excluded=[assignment.user],
    )


def _apply_assign(change) -> str:
    p = change.payload
    users = get_user_model().objects
    user = users.filter(email__iexact=p["email"]).first() or users.create_user(p["email"])
    role = Role.all_associations.get(association=change.association, pk=p["role"])
    services.assign_role(
        user,
        change.association,
        role,
        start=_date(p["start"]),
        end=_date(p["end"]),
        title=p["title"],
    )
    return reverse("access:index")


def _assignment(change) -> RoleAssignment:
    return RoleAssignment.all_associations.get(
        association=change.association, pk=change.payload["assignment"]
    )


def _apply_edit(change) -> str:
    p = change.payload
    services.update_assignment(
        _assignment(change), title=p["title"], start=_date(p["start"]), end=_date(p["end"])
    )
    return reverse("access:index")


def _apply_end(change) -> str:
    services.end_assignment(_assignment(change))
    return reverse("access:index")


def register() -> None:
    kinds.register(kinds.Kind(ASSIGN, gettext_lazy("Role assignment"), _apply_assign))
    kinds.register(kinds.Kind(EDIT, gettext_lazy("Role change"), _apply_edit))
    kinds.register(kinds.Kind(END, gettext_lazy("Role ending"), _apply_end))
