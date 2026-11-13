"""Test helpers: give a user a role without going through the UI."""

from homeoa.core.models import Association

from .services import assign_role, ensure_default_roles


def grant(user, role: str = "board", association=None, **kwargs):
    """Assign ``role`` (default board) in ``association`` (default: the first one, created
    if none exists) and return the assignment.

    Money access (``finance.manage``): ``grant(user, title="treasurer")``."""
    if association is None:
        association = Association.objects.order_by("pk").first()
    if association is None:
        association = Association.objects.create(name="Test HOA", short_name="T")
    ensure_default_roles(association)
    return assign_role(user, association, role, **kwargs)


def open_residents(association):
    """Make ``association`` live (since long ago, unless it already is) with resident access
    not paused, so owner and tenant roles grant their permissions. Returns the association."""
    import datetime

    from homeoa.core import status

    if not status.is_live(association):
        status.set_go_live_date(association, datetime.date(2000, 1, 1), reason="test")
    status.resume_residents(association, reason="test")
    return association


def close_residents(association):
    """Pause resident access for ``association`` (it may still be live)."""
    from homeoa.core import status

    status.pause_residents(association, reason="test")
    return association


def make_live(association, since=None):
    """Give ``association`` a go-live date (default long ago) so jobs that wait for go-live
    run. Leaves resident access and its pause alone. Returns the association."""
    import datetime

    from homeoa.core import status

    status.set_go_live_date(association, since or datetime.date(2000, 1, 1), reason="test")
    return association
