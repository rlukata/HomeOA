"""Audit-ready auth signals. Django's own ``user_logged_in``/``user_logged_out`` also fire.

``homeoa.audit.receivers`` records every one of them, and ``homeoa.accounts.events``
republishes the main ones as ``user.*`` domain events.
Every signal is sent with ``sender=None`` plus the keyword arguments noted below.
"""

from django.dispatch import Signal

login_link_requested = Signal()  # email, ip, user (None when unknown), throttled
login_link_rejected = Signal()  # reason ("invalid"|"used"|"expired"), ip, user
mfa_enrolled = Signal()  # user, request
mfa_disabled = Signal()  # user, request
mfa_challenge_passed = Signal()  # user, request, method ("totp"|"recovery")
mfa_challenge_failed = Signal()  # user, request
mfa_lockout = Signal()  # user, request (too many failed codes; the pending login is dropped)
recovery_codes_regenerated = Signal()  # user, request
passkey_registered = Signal()  # user, request, passkey
passkey_renamed = Signal()  # user, request, passkey
passkey_removed = Signal()  # user, request, passkey
passkey_login = Signal()  # user, request, passkey (passkey as the first and only step)
passkey_failed = Signal()  # user (None when unknown), request, reason
