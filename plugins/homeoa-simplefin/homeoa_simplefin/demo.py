"""Connect a sandbox (demo) association to SimpleFIN's real demo bridge.

``connect_demo(association)`` claims ``HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN`` only when
``HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE`` is on, the token is set, its claim URL is on a
demo host (``HOMEOA_SIMPLEFIN_DEMO_HOSTS``) and the association has no stored access URL
yet. It returns True when the association is connected to the real demo bridge, False
when the caller should fall back to the fake bridge. The token is one-time and never
logged.
"""

from __future__ import annotations

import logging

from django.conf import settings

from homeoa.plugins import services as plugins

from . import sync as sf
from .client import SimpleFinError, decode_setup_token

logger = logging.getLogger(__name__)


def connect_demo(association) -> bool:
    if not getattr(settings, "HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE", False):
        return False
    inst = plugins.installation_for(association, sf.PLUGIN_ID)
    stored = (inst.secrets or {}).get("access_url") if inst else None
    if stored:
        # Never re-claim: an existing real demo connection is kept as is.
        return sf.demo_host_allowed(stored)
    token = (getattr(settings, "HOMEOA_SIMPLEFIN_DEMO_SETUP_TOKEN", "") or "").strip()
    if not token:
        logger.info("simplefin demo: no demo setup token; using the fake bridge")
        return False
    try:
        claim_url = decode_setup_token(token)
    except SimpleFinError:
        logger.warning("simplefin demo: setup token is malformed; using the fake bridge")
        return False
    if not sf.demo_host_allowed(claim_url):
        logger.warning("simplefin demo: claim host is not a demo host; using the fake bridge")
        return False
    try:
        result = sf.connect(association, token)
    except SimpleFinError as exc:
        # Log the error kind only: a network error's text could carry the claim URL,
        # which is the token decoded.
        logger.warning(
            "simplefin demo: claim failed (%s: %s); using the fake bridge. Put a fresh "
            "demo setup token in .env and reseed.",
            type(exc).__name__,
            "already claimed or invalid (403)" if exc.kind == "claim" else exc.kind,
        )
        return False
    if not result.ok:
        logger.warning("simplefin demo: claimed, but first sync failed: %s", result.error)
    logger.info("simplefin demo: connected to the real demo bridge")
    return True
