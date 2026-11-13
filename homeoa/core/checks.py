"""Deployment system checks."""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Warning, register


@register("homeoa")
def check_base_url(app_configs=None, **kwargs):
    """HOMEOA_BASE_URL is the public origin for emailed links; prod must set it (https)."""
    if not getattr(settings, "HOMEOA_BASE_URL_REQUIRED", False):
        return []
    url = getattr(settings, "HOMEOA_BASE_URL", "")
    parts = urlsplit(url)
    if not url or parts.scheme != "https" or not parts.netloc:
        return [
            Error(
                "HOMEOA_BASE_URL must be set to the site's https origin "
                "(e.g. https://hoa.example.org).",
                id="homeoa.E001",
            )
        ]
    return []


@register("homeoa", deploy=True)
def check_backup_recipients(app_configs=None, **kwargs):
    """Backups should be decryptable by at least two board members (custody, RUNBOOK)."""
    minimum = getattr(settings, "HOMEOA_BACKUP_MIN_RECIPIENTS", 0)
    recipients = getattr(settings, "HOMEOA_BACKUP_AGE_RECIPIENTS", [])
    if len(recipients) < minimum:
        return [
            Warning(
                f"BACKUP_AGE_RECIPIENTS lists {len(recipients)} age key(s); at least "
                f"{minimum} are required so backups survive losing one key holder.",
                id="homeoa.W002",
            )
        ]
    return []
