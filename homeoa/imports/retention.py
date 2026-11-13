"""Import-file retention (REG-05 privacy): purge raw file contents of committed runs.

The run record, its mapping, report (row counts) and each file's name, size and SHA-256
stay; only the uploaded text is emptied. The retention period is the association setting
``import_retention_days`` (default 90). Comparison runs keep their parsed snapshot, which
the parallel-run diff reads, so they are never purged here.
"""

from __future__ import annotations

import datetime
import hashlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from homeoa import audit

from .models import ImportRun

SETTINGS_KEY = "import_retention_days"
DEFAULT_RETENTION_DAYS = 90


def retention_days(association) -> int:
    try:
        days = int((association.settings or {}).get(SETTINGS_KEY, DEFAULT_RETENTION_DAYS))
    except TypeError, ValueError:
        return DEFAULT_RETENTION_DAYS
    return max(days, 0)


def can_purge(run: ImportRun) -> bool:
    return run.status == ImportRun.Status.COMMITTED and run.purged_at is None


@transaction.atomic
def purge_run(run: ImportRun, *, reason: str = "manual", actor=None) -> bool:
    """Empty a committed run's file contents. Returns False when there is nothing to do."""
    run = ImportRun.all_associations.select_for_update().get(pk=run.pk)
    if not can_purge(run):
        return False
    files = []
    for f in run.files.all():
        if not f.sha256:
            f.sha256 = hashlib.sha256(f.content.encode("utf-8")).hexdigest()
        if f.size is None:
            f.size = len(f.content)
        f.content = ""
        f.save(update_fields=["content", "sha256", "size"])
        files.append({"name": f.name, "sha256": f.sha256})
    run.purged_at = timezone.now()
    run.save(update_fields=["purged_at"])
    audit.record(
        "imports.files_purged",
        run,
        actor=actor,
        changes={"files": files, "reason": reason},
        message=f"Purged {len(files)} import file(s) ({reason}).",
    )
    return True


def _zone(association):
    try:
        return ZoneInfo(association.timezone)
    except ZoneInfoNotFoundError, ValueError, TypeError:
        return datetime.UTC


def purge_expired(association, as_of: datetime.date) -> int:
    """Purge committed runs finished at least the retention period before ``as_of``."""
    cutoff = as_of - datetime.timedelta(days=retention_days(association))
    tz = _zone(association)
    runs = ImportRun.all_associations.filter(
        association=association,
        status=ImportRun.Status.COMMITTED,
        purged_at__isnull=True,
        finished_at__isnull=False,
    )
    count = 0
    for run in runs:
        if timezone.localtime(run.finished_at, tz).date() <= cutoff:
            count += purge_run(run, reason="retention")
    return count
