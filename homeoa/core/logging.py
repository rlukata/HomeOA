"""Structured JSON logging (OPS-03) and the trusted-proxy client IP."""

from __future__ import annotations

import ipaddress
import json
import logging
from datetime import UTC, datetime

from django.conf import settings

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "request_id"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, request_id, extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        for key, value in vars(record).items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def logging_config(*, level: str = "INFO", mail_admins: bool = False) -> dict:
    """LOGGING dict: JSON to stdout; optionally email ADMINS on request errors."""
    handlers: dict = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_id"],
        },
    }
    django_handlers = ["console"]
    if mail_admins:
        handlers["mail_admins"] = {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "filters": ["request_id", "require_debug_false"],
        }
        django_handlers.append("mail_admins")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "request_id": {"()": "homeoa.audit.context.RequestIdLogFilter"},
            "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        },
        "formatters": {"json": {"()": "homeoa.core.logging.JsonFormatter"}},
        "handlers": handlers,
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            "django": {"handlers": django_handlers, "level": level, "propagate": False},
        },
    }


def _networks(setting: str):
    nets = []
    for item in getattr(settings, setting, []):
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def _in(addr: str, nets) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def _cf_ip(request, fallback: str) -> str:
    """CF-Connecting-IP if it is a valid IP, else the tunnel hop itself."""
    value = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return fallback
    return value


def client_ip(request) -> str | None:
    """The visitor's IP address.

    REMOTE_ADDR, unless it is a configured trusted proxy (HOMEOA_TRUSTED_PROXIES):
    then the right-most X-Forwarded-For hop that is not itself a trusted proxy.
    If the chain of trusted hops reaches a Cloudflare Tunnel connector
    (HOMEOA_CF_TUNNEL_PROXIES), CF-Connecting-IP is used instead; from any other
    peer that header is ignored, so it cannot be spoofed.
    """
    remote = request.META.get("REMOTE_ADDR") or None
    if not remote:
        return remote
    tunnel = _networks("HOMEOA_CF_TUNNEL_PROXIES")
    if _in(remote, tunnel):
        return _cf_ip(request, remote)
    nets = _networks("HOMEOA_TRUSTED_PROXIES")
    if not nets or not _in(remote, nets):
        return remote
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return remote
        if _in(hop, tunnel):
            return _cf_ip(request, hop)
        if not _in(hop, nets):
            return hop
    return remote
