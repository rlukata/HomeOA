"""``user.*`` domain events, republished from the accounts and Django auth signals.

Accounts are user-level, so ``association_id`` is the association the request was
in, or ``None`` when there was none (e.g. a failed link outside any association).

* ``user.logged_in``    (user_id)
* ``user.logged_out``   (user_id, None for an anonymous logout)
* ``user.mfa_enrolled`` (user_id)
* ``user.mfa_disabled`` (user_id)
* ``user.login_failed`` (user_id or None, reason: "invalid" | "used" | "expired" |
  "mfa" | "mfa_lockout" | "passkey_invalid" | "passkey_unknown" | "passkey_counter").
  Never carries the email address that was tried.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.signals import user_logged_in, user_logged_out

from homeoa.core.context import get_current_association
from homeoa.events import Event, publish

from . import signals


@dataclass(frozen=True, kw_only=True)
class UserLoggedIn(Event):
    name = "user.logged_in"
    user_id: int


@dataclass(frozen=True, kw_only=True)
class UserLoggedOut(Event):
    name = "user.logged_out"
    user_id: int | None


@dataclass(frozen=True, kw_only=True)
class UserMFAEnrolled(Event):
    name = "user.mfa_enrolled"
    user_id: int


@dataclass(frozen=True, kw_only=True)
class UserMFADisabled(Event):
    name = "user.mfa_disabled"
    user_id: int


@dataclass(frozen=True, kw_only=True)
class UserLoginFailed(Event):
    name = "user.login_failed"
    user_id: int | None
    reason: str


def _assoc_id():
    assoc = get_current_association()
    return assoc.pk if assoc is not None else None


def _pk(user):
    return user.pk if user is not None and getattr(user, "is_authenticated", False) else None


def _logged_in(sender, request, user, **kwargs):
    publish(UserLoggedIn(association_id=_assoc_id(), user_id=user.pk))


def _logged_out(sender, request, user, **kwargs):
    publish(UserLoggedOut(association_id=_assoc_id(), user_id=_pk(user)))


def _enrolled(sender, user, **kwargs):
    publish(UserMFAEnrolled(association_id=_assoc_id(), user_id=user.pk))


def _disabled(sender, user, **kwargs):
    publish(UserMFADisabled(association_id=_assoc_id(), user_id=user.pk))


def _link_rejected(sender, reason, user=None, **kwargs):
    publish(UserLoginFailed(association_id=_assoc_id(), user_id=_pk(user), reason=reason))


def _mfa_failed(sender, user, **kwargs):
    publish(UserLoginFailed(association_id=_assoc_id(), user_id=user.pk, reason="mfa"))


def _mfa_lockout(sender, user, **kwargs):
    publish(UserLoginFailed(association_id=_assoc_id(), user_id=user.pk, reason="mfa_lockout"))


def _passkey_failed(sender, reason, user=None, **kwargs):
    publish(
        UserLoginFailed(association_id=_assoc_id(), user_id=_pk(user), reason=f"passkey_{reason}")
    )


def connect() -> None:
    """Called from ``AccountsConfig.ready``."""
    uid = "homeoa.accounts.events"
    user_logged_in.connect(_logged_in, dispatch_uid=f"{uid}.logged_in")
    user_logged_out.connect(_logged_out, dispatch_uid=f"{uid}.logged_out")
    signals.mfa_enrolled.connect(_enrolled, dispatch_uid=f"{uid}.enrolled")
    signals.mfa_disabled.connect(_disabled, dispatch_uid=f"{uid}.disabled")
    signals.login_link_rejected.connect(_link_rejected, dispatch_uid=f"{uid}.rejected")
    signals.mfa_challenge_failed.connect(_mfa_failed, dispatch_uid=f"{uid}.mfa_failed")
    signals.mfa_lockout.connect(_mfa_lockout, dispatch_uid=f"{uid}.mfa_lockout")
    signals.passkey_failed.connect(_passkey_failed, dispatch_uid=f"{uid}.passkey_failed")
