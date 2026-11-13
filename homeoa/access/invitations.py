"""Resident invitations, held until residents are switched on (POR-01).

* ``draft_invitation(person, by=None)``: create (or refresh the email of) a person's draft.
* ``send_invitation(invitation, by=None)``: refused with ``InvitationsHeld`` while the
  residents switch is off. When on, links the person to a login user (creating one for the
  email if needed) and queues the ``access.invitation`` notification via ``notify``.

The invitation email links to the sign-in page with the address filled in; the resident
then gets a one-time magic link there. A raw magic-link token is never put in the
message, because notification bodies are stored (tokens are only kept hashed).

Lifecycle:

* ``resend_invitation``: send a sent invitation again (e.g. expired or lost). Same rules
  as sending: held while residents are off; audited ``invitation.resent``.
* ``revoke_invitation``: void a draft or sent invitation; audited ``invitation.revoked``.
  Revoking does not remove a person's access: access always follows the registry
  (ownership/occupancy), and the invite carries no token to cancel. Drafting again
  turns a revoked invitation back into a draft (audited ``invitation.redrafted``).
* Expiry: a sent invitation is *expired* ``expiry_days(association)`` days after its
  last send. Default 30 days, overridable per association in
  ``association.settings["invitation_expiry_days"]``. Expiry is informational, since
  the link is just the sign-in page; it tells the board to resend.

Views: ``draft_view`` (one person) and ``send_view`` (all drafts of a unit), POST with
``units.manage``,, used by the unit page
section ``access/_unit_access.html``.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.translation import gettext as _t
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from homeoa import audit
from homeoa.notify import types as ntypes

from . import permissions as perms
from .decorators import require_perm
from .models import Invitation
from .services import residents_enabled

TYPE_KEY = "access.invitation"

INVITATION_TYPE = ntypes.NotificationType(
    key=TYPE_KEY,
    label=_("Invitation to the resident portal"),
    default_channels=("email",),
    audience=ntypes.AUDIENCE_RESIDENTS,
    # Not a legal notice, and the way owners reach the consent page.
    needs_consent=False,
    subject_template="You're invited to {{ association_name }} online",
    body_template=(
        "Hello {{ recipient.full_name }},\n\n"
        "{{ association_name }} now has a resident website. Sign in with your email "
        "address ({{ email }}) here; we'll email you a one-time sign-in link:\n\n"
        "{{ login_url }}\n"
        "{% if owner %}\nOnce you're in, your welcome packet has the governing documents, "
        "who's on the board and how to pay dues.\n{% endif %}"
    ),
)


EXPIRY_DAYS_KEY = "invitation_expiry_days"
DEFAULT_EXPIRY_DAYS = 30


def expiry_days(association) -> int:
    """Days until a sent invitation is shown as expired (association setting, default 30)."""
    raw = (getattr(association, "settings", None) or {}).get(EXPIRY_DAYS_KEY)
    try:
        days = int(raw)
    except TypeError, ValueError:
        return DEFAULT_EXPIRY_DAYS
    return days if days > 0 else DEFAULT_EXPIRY_DAYS


@transaction.atomic
def set_expiry_days(association, days: int, by=None) -> None:
    """Change the association's invitation expiry (audited)."""
    days = int(days)
    if days < 1:
        raise InvitationError(_t("Expiry must be at least one day."))
    old = expiry_days(association)
    settings = dict(association.settings or {})
    settings[EXPIRY_DAYS_KEY] = days
    association.settings = settings
    association.save(update_fields=["settings"])
    audit.record(
        "association.invitation_expiry",
        association,
        actor=by,
        changes={EXPIRY_DAYS_KEY: [old, days]},
    )


class InvitationsHeld(Exception):
    """Sending refused: residents are switched off."""


class InvitationError(ValueError):
    pass


def register_type() -> None:
    ntypes.register(INVITATION_TYPE)


def draft_invitation(person, by=None) -> Invitation:
    email = (person.email or "").strip()
    if not email:
        raise InvitationError(_t("%(name)s has no email address.") % {"name": person.full_name})
    inv, created = Invitation.all_associations.get_or_create(
        association=person.association,
        person=person,
        defaults={"email": email, "created_by": by},
    )
    if not created and inv.status == Invitation.Status.REVOKED:
        inv.status = Invitation.Status.DRAFT
        inv.email = email
        inv.revoked_at = None
        inv.revoked_by = None
        inv.save(update_fields=["status", "email", "revoked_at", "revoked_by"])
        audit.record("invitation.redrafted", inv, actor=by, changes={"email": email})
    elif not created and inv.status == Invitation.Status.DRAFT and inv.email != email:
        inv.email = email
        inv.save(update_fields=["email"])
    if created:
        audit.record("invitation.drafted", inv, actor=by, changes={"email": email})
    return inv


def _login_user(person, email):
    if person.user_id:
        return person.user
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(email=email)
    person.user = user
    person.save(update_fields=["user"])
    return user


def _is_owner(person) -> bool:
    from homeoa.registry.services import units_owned_by

    return bool(units_owned_by(person))


def _deliver(invitation: Invitation, by, login_url: str, action: str):
    from homeoa.notify.services import notify

    association = invitation.association
    _login_user(invitation.person, invitation.email)
    url = login_url or reverse("accounts:login")
    url = f"{url}?{urlencode({'email': invitation.email})}"
    notes = notify(
        TYPE_KEY,
        [invitation.person],
        {
            "association_name": association.name,
            "email": invitation.email,
            "login_url": url,
            "owner": _is_owner(invitation.person),
        },
        obj=invitation,
        # One notification per send: the first keeps the original key, resends get their own.
        period=f"send-{invitation.send_count + 1}" if invitation.send_count else "",
        association=association,
    )
    invitation.status = Invitation.Status.SENT
    invitation.sent_at = timezone.now()
    invitation.sent_by = by
    invitation.send_count += 1
    invitation.save(update_fields=["email", "status", "sent_at", "sent_by", "send_count"])
    audit.record(
        action,
        invitation,
        actor=by,
        changes={"email": invitation.email, "send_count": invitation.send_count},
    )
    return notes


@transaction.atomic
def send_invitation(invitation: Invitation, by=None, *, login_url: str = ""):
    """Queue the invitation. Returns the ``notify`` notifications (empty when no channel
    is left, e.g. the person switched invitations off)."""
    if not residents_enabled(invitation.association):
        raise InvitationsHeld
    if invitation.status == Invitation.Status.SENT:
        raise InvitationError(_t("This invitation was already sent."))
    if invitation.status == Invitation.Status.REVOKED:
        raise InvitationError(_t("This invitation was revoked."))
    return _deliver(invitation, by, login_url, "invitation.sent")


@transaction.atomic
def resend_invitation(invitation: Invitation, by=None, *, login_url: str = ""):
    """Send an already-sent invitation again; restarts its expiry clock."""
    if not residents_enabled(invitation.association):
        raise InvitationsHeld
    if invitation.status != Invitation.Status.SENT:
        raise InvitationError(_t("Only a sent invitation can be resent."))
    email = (invitation.person.email or "").strip()
    if email:
        invitation.email = email  # follow the registry's current address
    return _deliver(invitation, by, login_url, "invitation.resent")


@transaction.atomic
def revoke_invitation(invitation: Invitation, by=None) -> Invitation:
    """Void a draft or sent invitation. Does not remove access (that follows the registry)."""
    if invitation.status == Invitation.Status.REVOKED:
        raise InvitationError(_t("This invitation was already revoked."))
    old = invitation.status
    invitation.status = Invitation.Status.REVOKED
    invitation.revoked_at = timezone.now()
    invitation.revoked_by = by
    invitation.save(update_fields=["status", "revoked_at", "revoked_by"])
    audit.record(
        "invitation.revoked",
        invitation,
        actor=by,
        changes={"status": [old, Invitation.Status.REVOKED.value], "email": invitation.email},
    )
    return invitation


# --- views ----------------------------------------------------------------------------


def _unit_url(number):
    return reverse("registry:unit", args=[number])


@require_POST
@require_perm(perms.UNITS_MANAGE)
def draft_view(request, number, person_id):
    from homeoa.registry.models import Person, Unit

    get_object_or_404(Unit.objects, number=number)
    person = get_object_or_404(Person.objects, pk=person_id)
    try:
        draft_invitation(person, by=request.user)
    except InvitationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _t("Invitation drafted for %(name)s.") % {"name": person})
    return redirect(_unit_url(number))


@require_POST
@require_perm(perms.UNITS_MANAGE)
def send_view(request, number):
    """Send every drafted invitation for the unit's current people."""
    from homeoa.registry.models import Unit
    from homeoa.registry.services import today_for

    unit = get_object_or_404(Unit.objects, number=number)
    if not residents_enabled(request.association):
        raise PermissionDenied(_t("Invitations are held until residents are switched on."))
    login_url = request.build_absolute_uri(reverse("accounts:login"))
    drafts = [
        inv
        for _p, inv in unit_people(unit, today_for(unit.association))
        if inv is not None and inv.status == Invitation.Status.DRAFT
    ]
    if not drafts:
        messages.info(request, _t("No drafted invitations to send."))
    for inv in drafts:
        notes = send_invitation(inv, by=request.user, login_url=login_url)
        if notes:
            messages.success(request, _t("Invitation sent to %(email)s.") % {"email": inv.email})
        else:
            messages.warning(
                request,
                _t("Marked as invited, but no email went to %(name)s.") % {"name": inv.person},
            )
    return redirect(_unit_url(number))


def _unit_invitation(number, invitation_id):
    from homeoa.registry.models import Unit
    from homeoa.registry.services import today_for

    unit = get_object_or_404(Unit.objects, number=number)
    for _p, inv in unit_people(unit, today_for(unit.association)):
        if inv is not None and inv.pk == invitation_id:
            return inv
    raise Http404


@require_POST
@require_perm(perms.UNITS_MANAGE)
def resend_view(request, number, invitation_id):
    inv = _unit_invitation(number, invitation_id)
    if not residents_enabled(request.association):
        raise PermissionDenied(_t("Invitations are held until residents are switched on."))
    login_url = request.build_absolute_uri(reverse("accounts:login"))
    try:
        notes = resend_invitation(inv, by=request.user, login_url=login_url)
    except InvitationError as exc:
        messages.error(request, str(exc))
    else:
        if notes:
            messages.success(request, _t("Invitation resent to %(email)s.") % {"email": inv.email})
        else:
            messages.warning(
                request,
                _t("Marked as resent, but no email went to %(name)s.") % {"name": inv.person},
            )
    return redirect(_unit_url(number))


@require_POST
@require_perm(perms.UNITS_MANAGE)
def revoke_view(request, number, invitation_id):
    inv = _unit_invitation(number, invitation_id)
    try:
        revoke_invitation(inv, by=request.user)
    except InvitationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _t("Invitation for %(name)s revoked.") % {"name": inv.person})
    return redirect(_unit_url(number))


def unit_people(unit, on):
    """Current owners and occupants of ``unit`` with their invitation (or ``None``)."""
    from homeoa.registry.models import Occupancy
    from homeoa.registry.services import current_owners

    people = list(current_owners(unit, on))
    occ = Occupancy.objects.filter(unit=unit, start__lte=on).exclude(end__lt=on)
    for o in occ.select_related("person"):
        if o.person not in people:
            people.append(o.person)
    invites = {i.person_id: i for i in Invitation.objects.filter(person__in=people)}
    return [(p, invites.get(p.pk)) for p in people]


urls = (
    [
        path("draft/<int:person_id>/", draft_view, name="draft"),
        path("send/", send_view, name="send"),
        path("<int:invitation_id>/resend/", resend_view, name="resend"),
        path("<int:invitation_id>/revoke/", revoke_view, name="revoke"),
    ],
    "access_invitations",
)
