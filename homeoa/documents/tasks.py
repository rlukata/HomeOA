"""Background jobs for documents: reading the text inside files for search (DOC-05).

``settings.HOMEOA_JOBS_INLINE`` (on in the ``dev`` and ``e2e`` settings) reads a new upload
in-process when its transaction commits, since ``runserver`` has no worker.
"""

from django.conf import settings
from django.db import transaction

from homeoa.jobs import app, periodic

# Versions read per association by one daily run (the backlog after an upgrade, or scans
# once OCR is turned on); the rest wait for the next day.
DAILY_LIMIT = 200


@app.task(name="documents.read_text", queue="default")
def read_text(version_id: int) -> None:
    """Read the text of a newly uploaded version."""
    from homeoa.audit import audit_context
    from homeoa.core.context import association_context

    from .models import DocumentVersion
    from .text import read

    version = (
        DocumentVersion.all_associations.select_related("association").filter(pk=version_id).first()
    )
    if version is None:
        return
    with association_context(version.association), audit_context(label="job:documents.read_text"):
        read(version)


def queue_read_text(version_id: int) -> None:
    """Read the version's text once the upload commits."""
    if getattr(settings, "HOMEOA_JOBS_INLINE", False):
        transaction.on_commit(lambda: read_text(version_id))
    else:
        read_text.defer(version_id=version_id)


@periodic("documents.read_waiting_text", cron="30 2 * * *", catch_up=False)
def read_waiting_text(association, as_of):
    """Read documents not read yet, and scans again once reading scans (OCR) is turned on."""
    from .text import read, waiting

    for version in waiting(association, limit=DAILY_LIMIT):
        read(version)
