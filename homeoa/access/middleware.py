"""Resident-access gate: while residents are off, resident-only users reach nothing but /me/.

Runs after ``CurrentAssociationMiddleware``. Board users, instance admins and anonymous
visitors pass through untouched.
"""

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

from .services import is_read_only, is_resident_locked

# Paths a locked resident may still open (prefix match).
_OPEN_NAMES = ("portal:home", "accounts:logout", "ui:manifest", "ui:service_worker", "ui:offline")


class ResidentGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self._open: tuple[str, ...] | None = None

    def _open_paths(self) -> tuple[str, ...]:
        if self._open is None:
            paths = [reverse(name) for name in _OPEN_NAMES]
            paths += ["/healthz", settings.STATIC_URL if settings.STATIC_URL else "/static/"]
            self._open = tuple(paths)
        return self._open

    def __call__(self, request):
        path = request.path_info
        if not any(path == p or path.startswith(p) for p in self._open_paths()):
            association = getattr(request, "association", None)
            if is_resident_locked(getattr(request, "user", None), association):
                return redirect("portal:home")
        return self.get_response(request)


# Paths a read-only user may still POST to (own account, sign-out, own inbox, /me/).
_READ_ONLY_OPEN = ("/logout/", "/login/", "/account/", "/notifications/", "/me/")


class ReadOnlyRoleMiddleware:
    """Read-only roles (the CPA) get 403 on every write, whatever the view checks.

    Applies when every hand-assigned role the user holds in the association is read-only;
    derived owner/tenant roles keep their own /me/ pages.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            path = request.path_info
            if not path.startswith(_READ_ONLY_OPEN) and is_read_only(
                getattr(request, "user", None), getattr(request, "association", None)
            ):
                from django.core.exceptions import PermissionDenied

                raise PermissionDenied
        return self.get_response(request)
