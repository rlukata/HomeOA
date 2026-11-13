"""Board to-do "Run monthly parallel-run check" (FIN-18). The Home to-do registry renders it.

Shown from the 1st of each month, while the association is in a parallel run with its management
company (it has imported from them), until a check has been run that month.
"""

from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.core.context import association_today

from ..models import ImportRun, Source
from .models import DiffRun


def todo_items(association, today=None) -> list[dict]:
    today = today or association_today(association)
    if not ImportRun.all_associations.filter(
        association=association, source=Source.ECONDO
    ).exists():
        return []
    month_start = today.replace(day=1)
    if DiffRun.all_associations.filter(
        association=association, created_at__date__gte=month_start
    ).exists():
        return []
    return [
        {
            "id": f"parallel-run-{month_start:%Y-%m}",
            "kind": "money",
            "title": _("Run monthly parallel-run check"),
            "sub": _(
                "Load this month's export from the management company and compare it to HomeOA."
            ),
            "due": month_start,
            "url": reverse("imports:parallel_run"),
            "action": _("Open"),
        }
    ]
