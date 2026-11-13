"""Views declare the capability they need.

``@require_perm(perms.UNITS_MANAGE)`` (function views) or ``PermissionRequiredMixin`` with
``permission_required = ...`` (class views). Anonymous users go to login; signed-in users
without the permission get 403 (never a redirect). Several codes mean "any of".
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .services import has_any


def _allowed(request, codes) -> bool:
    return has_any(request.user, codes, association=getattr(request, "association", None))


def require_perm(*codes: str):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not _allowed(request, codes):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator


class PermissionRequiredMixin:
    permission_required: str | tuple[str, ...] = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        codes = self.permission_required
        codes = (codes,) if isinstance(codes, str) else tuple(codes)
        if not _allowed(request, codes):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


def resident_view(view=None, *, perm: str | None = None):
    """Resident (``/me/``) pages.

    Signed-in users only. While residents are off, everyone sees the "not open yet" page
    instead of the view. When on, ``perm`` (optional) must be held or the user gets 403.
    """

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            from django.shortcuts import render

            from .services import residents_enabled

            association = getattr(request, "association", None)
            if association is None or not residents_enabled(association):
                return render(request, "portal/closed.html", {"tabs": []}, status=403)
            if perm and not _allowed(request, (perm,)):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator(view) if view is not None else decorator
