import re
import uuid

from homeoa.core.logging import client_ip

from .context import AuditActor, reset_audit_actor, set_audit_actor

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _client_ip(request) -> str | None:
    # X-Forwarded-For is honoured only from HOMEOA_TRUSTED_PROXIES.
    return client_ip(request)


class AuditContextMiddleware:
    """Expose the acting user, client IP and a request id to audit events."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request.request_id = request_id
        # Keep the request, not the user: login()/logout() swap request.user mid-request.
        token = set_audit_actor(
            AuditActor(request=request, ip=_client_ip(request), request_id=request_id)
        )
        try:
            # Django converts exceptions below this middleware into error responses
            # (inside this context, so logged errors carry the request id).
            response = self.get_response(request)
        finally:
            reset_audit_actor(token)
        response.headers.setdefault("X-Request-ID", request_id)
        return response
