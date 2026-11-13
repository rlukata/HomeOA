import mimetypes

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from homeoa import providers
from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm

from .defaults.localdisk import LocalDiskStorage, resolve_token
from .storage import FileNotFound


@require_GET
def download(request, token: str):
    """Serve a local-disk file for a signed URL. The token is the capability; no login."""
    try:
        key, filename = resolve_token(token)
        data = LocalDiskStorage().get(key)
    except FileNotFound as exc:
        raise Http404 from exc
    name = filename or key.rsplit("/", 1)[-1].split("-", 1)[-1]
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = HttpResponse(data, content_type=content_type)
    safe_name = name.replace('"', "").replace("\r", "").replace("\n", "")
    response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_perm(perms.PLUGINS_MANAGE)
@require_POST
def choose(request):
    """Set which plugin serves an interface (form on /settings/plugins/)."""
    association = request.association
    kind = request.POST.get("kind", "")
    plugin_id = request.POST.get("plugin_id", "")
    if association is None or kind not in providers.DEFAULTS:
        raise Http404
    try:
        providers.set_choice(association, kind, plugin_id)
    except ValueError:
        messages.error(request, _("That plugin does not provide this service."))
    else:
        messages.success(request, _("Provider choice saved."))
    return redirect("plugins:index")
