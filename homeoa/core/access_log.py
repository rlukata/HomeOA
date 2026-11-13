"""Access log with the visitor's IP.

Behind Caddy (and cloudflared in tunnel mode) gunicorn only sees the proxy hop, so
production turns gunicorn's access log off (``deploy/entrypoint.sh``) and logs each
request here instead, with :func:`homeoa.core.logging.client_ip`: the same trusted-proxy
rule the audit log uses, so a spoofed ``CF-Connecting-IP`` or ``X-Forwarded-For`` from
an untrusted peer never becomes the client IP.
"""

from __future__ import annotations

import logging
import time

from .logging import client_ip

logger = logging.getLogger("homeoa.access")

QUIET_PATHS = ("/healthz",)  # container healthchecks every few seconds


class AccessLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        if not request.path.startswith(QUIET_PATHS):
            log_request(request, response, time.monotonic() - start)
        return response


def log_request(request, response, seconds: float) -> None:
    ip = client_ip(request) or "-"
    status = getattr(response, "status_code", 0)
    logger.info(
        '%s "%s %s" %s',
        ip,
        request.method,
        request.get_full_path(),
        status,
        extra={
            "client_ip": ip,
            "method": request.method,
            "path": request.path,
            "status": status,
            "duration_ms": round(seconds * 1000, 1),
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
        },
    )
