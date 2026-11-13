from django.shortcuts import render
from procrastinate.contrib.django.models import ProcrastinateJob

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm

from .models import ScheduledRun
from .scheduler import periodic_jobs

STATUS_BADGE = {
    "succeeded": "ok",
    "failed": "danger",
    "running": "info",
    "doing": "info",
    "cancelled": "warn",
    "aborted": "warn",
}


@require_perm(perms.JOBS_READ)
def index(request):
    """Read-only job history for the board."""
    from homeoa.core import status
    from homeoa.core.context import get_current_association

    association = get_current_association()
    runs = list(ScheduledRun.objects.all()[:100])
    for run in runs:
        run.badge = STATUS_BADGE.get(run.status, "")
    queued = list(ProcrastinateJob.objects.order_by("-id")[:50])
    for job in queued:
        job.badge = STATUS_BADGE.get(job.status, "")
    return render(
        request,
        "jobs/index.html",
        {
            "current_hub": None,
            "subtabs": [],
            "periodic": list(periodic_jobs().values()),
            "runs": runs,
            "queued": queued,
            "setting_up": association is not None and not status.is_live(association),
        },
    )
