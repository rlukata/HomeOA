"""Operational health: backup staleness (OPS-02 alerting without webhooks).

The backup container writes ``last_success.json`` / ``last_failure.json`` into a small
shared volume (``HOMEOA_BACKUP_STATUS_DIR``). A daily job reads them; when the last
success is older than ``HOMEOA_BACKUP_MAX_AGE_HOURS`` (default 36) or a failure is newer
than the last success, it emails ADMINS and publishes ``ops.backup_stale``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.mail import mail_admins
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from homeoa.events import Event, publish

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class BackupStale(Event):
    """Backups are missing, old or failing (shown as a board to-do)."""

    name = "ops.backup_stale"
    reason: str
    last_success: str = ""
    last_failure: str = ""


@dataclass(frozen=True)
class BackupStatus:
    stale: bool
    reason: str = ""
    last_success: dt.datetime | None = None
    last_failure: dt.datetime | None = None


def _read_time(path: Path) -> dt.datetime | None:
    try:
        data = json.loads(path.read_text())
        value = dt.datetime.fromisoformat(data["time"])
    except OSError, ValueError, KeyError, TypeError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def backup_status(now: dt.datetime | None = None) -> BackupStatus | None:
    """None when no status directory is configured (dev, tests)."""
    directory = getattr(settings, "HOMEOA_BACKUP_STATUS_DIR", "")
    if not directory:
        return None
    now = now or timezone.now()
    root = Path(directory)
    success = _read_time(root / "last_success.json")
    failure = _read_time(root / "last_failure.json")
    max_age = dt.timedelta(hours=int(getattr(settings, "HOMEOA_BACKUP_MAX_AGE_HOURS", 36)))
    if failure and (success is None or failure > success):
        reason = "failed"
    elif success is None:
        reason = "never"
    elif now - success > max_age:
        reason = "old"
    else:
        return BackupStatus(False, last_success=success, last_failure=failure)
    return BackupStatus(True, reason, success, failure)


_MESSAGES = {
    "failed": gettext_noop("The most recent backup failed."),
    "never": gettext_noop("No successful backup has been recorded."),
    "old": gettext_noop("The last successful backup is older than %(hours)s hours."),
}


def check_backups(association, now: dt.datetime | None = None) -> BackupStatus | None:
    status = backup_status(now)
    if status is None or not status.stale:
        return status
    hours = getattr(settings, "HOMEOA_BACKUP_MAX_AGE_HOURS", 36)
    message = _(_MESSAGES[status.reason]) % {"hours": hours}
    detail = _("Last success: %(s)s. Last failure: %(f)s.") % {
        "s": status.last_success.isoformat() if status.last_success else "-",
        "f": status.last_failure.isoformat() if status.last_failure else "-",
    }
    logger.error("backup stale: %s", status.reason, extra={"action": "backup_stale"})
    mail_admins(_("Backups need attention"), f"{message}\n{detail}\n", fail_silently=True)
    publish(
        BackupStale(
            association_id=association.pk,
            reason=status.reason,
            last_success=status.last_success.isoformat() if status.last_success else "",
            last_failure=status.last_failure.isoformat() if status.last_failure else "",
        )
    )
    return status
