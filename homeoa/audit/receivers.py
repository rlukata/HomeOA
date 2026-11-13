"""Audit receivers for the accounts signals and Django's login/logout (PLT-04).

Actions (all ``object_type="accounts.user"``):

``auth.login`` / ``auth.logout``            Django ``user_logged_in`` / ``user_logged_out``
``auth.link_requested``                     login form submitted (``throttled`` in changes)
``auth.link_rejected``                      invalid/used/expired link (``reason``)
``auth.mfa_enrolled`` / ``auth.mfa_disabled``
``auth.mfa_passed`` (``method``) / ``auth.mfa_failed`` / ``auth.mfa_lockout``
``auth.recovery_codes_regenerated``
``auth.passkey_registered`` / ``auth.passkey_renamed`` / ``auth.passkey_removed`` (``name``)
``auth.passkey_login`` (``name``) / ``auth.passkey_failed`` (``reason``)

Failed or rejected attempts are recorded with ``association=NULL``: they happen before
anyone is known to belong to an association. A link request for an unknown address
records no user, and its event looks exactly like a known address's apart from the
object id, so the log is no "does this account exist" oracle beyond what the board sees.
Successful events keep the request's current association.
"""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in, user_logged_out

from homeoa.accounts import signals
from homeoa.core.context import association_context

from .services import record

USER = "accounts.user"


def _record(action, user=None, *, actor=None, null_association=False, **changes):
    kwargs = {
        "object_type": USER,
        "object_id": user.pk if user is not None else None,
        "object_repr": str(user) if user is not None else "",
        "changes": changes,
        "actor": actor,
    }
    if null_association:
        with association_context(None):
            return record(action, **kwargs)
    return record(action, **kwargs)


def _known(user):
    return user if user is not None and getattr(user, "is_authenticated", False) else None


def on_logged_in(sender, request, user, **kwargs):
    _record("auth.login", user, actor=user)


def on_logged_out(sender, request, user, **kwargs):
    user = _known(user)
    _record("auth.logout", user, actor=user)


def on_link_requested(sender, email, ip=None, user=None, throttled=False, **kwargs):
    # No email in the record: an unknown address must leave no trace that hints at it.
    _record("auth.link_requested", user, null_association=True, throttled=bool(throttled))


def on_link_rejected(sender, reason, ip=None, user=None, **kwargs):
    _record("auth.link_rejected", user, null_association=True, reason=reason)


def on_mfa_enrolled(sender, user, **kwargs):
    _record("auth.mfa_enrolled", user)


def on_mfa_disabled(sender, user, **kwargs):
    _record("auth.mfa_disabled", user)


def on_mfa_passed(sender, user, method="", **kwargs):
    _record("auth.mfa_passed", user, method=method)


def on_mfa_failed(sender, user, **kwargs):
    _record("auth.mfa_failed", user, null_association=True)


def on_mfa_lockout(sender, user, **kwargs):
    _record("auth.mfa_lockout", user, null_association=True)


def on_recovery_codes_regenerated(sender, user, **kwargs):
    _record("auth.recovery_codes_regenerated", user)


def on_passkey_registered(sender, user, passkey, **kwargs):
    _record("auth.passkey_registered", user, name=passkey.name)


def on_passkey_renamed(sender, user, passkey, **kwargs):
    _record("auth.passkey_renamed", user, name=passkey.name)


def on_passkey_removed(sender, user, passkey, **kwargs):
    _record("auth.passkey_removed", user, name=passkey.name)


def on_passkey_login(sender, user, passkey, **kwargs):
    _record("auth.passkey_login", user, actor=user, name=passkey.name)


def on_passkey_failed(sender, reason, user=None, **kwargs):
    _record("auth.passkey_failed", _known(user), null_association=True, reason=reason)


CONNECTIONS = [
    (user_logged_in, on_logged_in),
    (user_logged_out, on_logged_out),
    (signals.login_link_requested, on_link_requested),
    (signals.login_link_rejected, on_link_rejected),
    (signals.mfa_enrolled, on_mfa_enrolled),
    (signals.mfa_disabled, on_mfa_disabled),
    (signals.mfa_challenge_passed, on_mfa_passed),
    (signals.mfa_challenge_failed, on_mfa_failed),
    (signals.mfa_lockout, on_mfa_lockout),
    (signals.recovery_codes_regenerated, on_recovery_codes_regenerated),
    (signals.passkey_registered, on_passkey_registered),
    (signals.passkey_renamed, on_passkey_renamed),
    (signals.passkey_removed, on_passkey_removed),
    (signals.passkey_login, on_passkey_login),
    (signals.passkey_failed, on_passkey_failed),
]


def connect() -> None:
    """Called from ``AuditConfig.ready``."""
    for signal, receiver in CONNECTIONS:
        signal.connect(receiver, dispatch_uid=f"homeoa.audit.receivers.{receiver.__name__}")
