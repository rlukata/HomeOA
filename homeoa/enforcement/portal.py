"""Violations at the resident's units, shown on the resident Requests tab."""

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.portal.contributions import PortalPart


def requests_context(context):
    """The violations to list, or ``None`` to hide the section (also in the board's preview)."""
    from homeoa.enforcement.responses import visible_to

    request = context.get("request")
    if context.get("read_only") or request is None:
        return None
    assoc = request.association
    if not has_perm(request.user, perms.VIOLATIONS_OWN, association=assoc):
        return None
    violations = list(visible_to(request.user, assoc))
    return {"violations": violations} if violations else None


REQUESTS_PART = PortalPart("requests", "enforcement/_portal_requests.html", requests_context)
