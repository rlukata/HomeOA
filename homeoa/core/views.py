import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import sandbox
from .context import association_context
from .forms import NewAssociationForm
from .models import Association

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def healthz(request):
    """Liveness plus DB check (OPS-06): 200 when the database answers, else 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        logger.warning("healthz: database check failed", exc_info=True)
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


@login_required
def new_association(request):
    """A new install's first step: the System admin names the association. Only while none
    exists; after that the page sends everyone Home."""
    if not request.user.is_superuser:
        raise PermissionDenied
    if Association.objects.exists():
        return redirect("dashboard:index")
    form = NewAssociationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _create(form)
        messages.success(
            request,
            _(
                "Association added. Next: turn on the plugins you use, choose the state "
                "pack, and add the board in Roles."
            ),
        )
        return redirect("ui:settings")
    return render(
        request,
        "core/new_association.html",
        {"current_hub": None, "subtabs": [], "form": form},
    )


@transaction.atomic
def _create(form) -> Association:
    """Save the association with what every association starts with: the default roles
    and the chart of accounts. On a demo server it is a sandbox."""
    from homeoa.access.services import ensure_default_roles
    from homeoa.ledger.chart import load_chart_of_accounts

    association = form.save()
    if settings.HOMEOA_SANDBOX_NEW_ASSOCIATIONS:
        sandbox.enable(association)
    with association_context(association):
        ensure_default_roles(association)
        load_chart_of_accounts(association)
    return association
