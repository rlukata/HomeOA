import logging

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from homeoa import audit
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.core.context import association_context
from homeoa.core.models import Association

from . import registry, services
from .forms import PluginSettingsForm
from .models import PluginInstallation, WebhookDelivery

logger = logging.getLogger(__name__)


@require_perm(perms.PLUGINS_MANAGE)
def plugin_list(request):
    association = request.association
    installs = {
        i.plugin_id: i for i in PluginInstallation.all_associations.filter(association=association)
    }
    rows = [{"manifest": m, "installation": installs.get(m.id)} for m in registry.all_plugins()]
    return render(
        request,
        "plugins/list.html",
        {"current_hub": None, "subtabs": [], "rows": rows},
    )


@require_perm(perms.PLUGINS_MANAGE)
def plugin_detail(request, plugin_id: str):
    manifest = registry.get(plugin_id)
    if manifest is None or request.association is None:
        raise Http404
    installation = services.installation_for(request.association, plugin_id)
    if request.method == "POST":
        form = PluginSettingsForm(manifest, installation, data=request.POST)
        if form.is_valid():
            try:
                services.save_installation(
                    request.association,
                    plugin_id,
                    enabled=form.cleaned_data["enabled"],
                    data=form.settings_data(),
                )
            except services.SettingsError as exc:
                for field, errs in exc.errors.items():
                    for err in errs:
                        form.add_error(field if field in form.fields else None, err)
            else:
                messages.success(request, _("Plugin settings saved."))
                return redirect("plugins:detail", plugin_id=plugin_id)
    else:
        form = PluginSettingsForm(manifest, installation)
    return render(
        request,
        "plugins/detail.html",
        {
            "current_hub": None,
            "subtabs": [],
            "manifest": manifest,
            "installation": installation,
            "form": form,
        },
    )


@csrf_exempt
@require_POST
def webhook(request, plugin_id: str, association_id: int):
    manifest = registry.get(plugin_id)
    if manifest is None or not manifest.webhook:
        raise Http404
    association = Association.objects.filter(pk=association_id).first()
    if association is None:
        raise Http404
    with (
        association_context(association),
        audit.audit_context(actor=None, label=f"webhook:{plugin_id}"),
    ):
        installation = services.installation_for(association, plugin_id)
        if installation is None or not installation.enabled:
            raise Http404
        handler = import_string(manifest.webhook)
        if isinstance(handler, type):
            handler = handler()
        if not handler.verify_signature(request, installation):
            return HttpResponseBadRequest("bad signature")
        event_id = str(handler.event_id(request) or "")
        if not event_id:
            return HttpResponseBadRequest("missing event id")
        try:
            with transaction.atomic():
                WebhookDelivery.objects.create(
                    association=association, plugin_id=plugin_id, event_id=event_id
                )
                handler.handle(request, installation)
        except IntegrityError:
            return HttpResponse("duplicate", content_type="text/plain")
        except Exception:
            logger.exception("Webhook %s/%s failed for %s", plugin_id, event_id, association.pk)
            return HttpResponse("error", status=500, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
