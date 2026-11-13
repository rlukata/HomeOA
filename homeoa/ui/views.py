from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render

from .nav import get_hub


def shell_gate(view):
    """Shell pages require a signed-in user."""
    return login_required(view)


def _render_shell(request, hub_id=None, tab_slug=None, title=None):
    hub = get_hub(hub_id, getattr(request, "association", None)) if hub_id else None
    if hub_id and hub is None:
        raise Http404
    if hub is not None and not hub.is_visible(request):
        if hub.id == "home":
            # Residents (and anyone without the board workspace) land on /me/.
            return redirect("portal:home")
        raise PermissionDenied
    slugs = [t.slug for t in hub.subtabs] if hub else []
    if tab_slug in slugs:
        index = slugs.index(tab_slug)
    else:
        # A titled page (e.g. Assessments) sits under the hub without owning a sub-tab.
        index = 0 if tab_slug is None and title is None else -1
    tabs = (
        [
            {"label": t.label, "url": hub.subtab_url(i), "on": i == index}
            for i, t in enumerate(hub.subtabs)
        ]
        if hub
        else []
    )
    current_tab = hub.subtabs[index] if hub and hub.subtabs and index >= 0 else None
    heading = title or (current_tab.label if current_tab else (hub.label if hub else ""))
    return render(
        request,
        "ui/hub.html",
        {"current_hub": hub, "subtabs": tabs, "current_tab": current_tab, "heading": heading},
    )


@shell_gate
def placeholder(request, hub_id=None, tab_slug=None, title=None):
    """Stand-in page that app URLconfs point at until the app builds its real view."""
    return _render_shell(request, hub_id, tab_slug, title)


@shell_gate
def hub(request, hub_id: str):
    if hub_id == "home":
        raise Http404
    return _render_shell(request, hub_id)


@shell_gate
def subtab(request, hub_id: str, tab_slug: str):
    # A section of a module that is off is not among the hub's sections, so it 404s.
    hub_obj = get_hub(hub_id, getattr(request, "association", None))
    slugs = [t.slug for t in hub_obj.subtabs] if hub_obj else []
    if tab_slug not in slugs or slugs.index(tab_slug) == 0:
        raise Http404
    return _render_shell(request, hub_id, tab_slug)


@shell_gate
def settings_page(request):
    return render(request, "ui/settings.html", {"current_hub": None, "subtabs": []})


# ---------------------------------------------------------------- PWA (PLT-09)
# Public on purpose: browsers fetch these without a session.


def manifest(request):
    """Web app manifest so browsers offer "Install" (Lighthouse installability)."""
    from django.http import JsonResponse
    from django.templatetags.static import static
    from django.urls import reverse
    from django.utils.translation import gettext as _

    icons = [
        {"src": static("homeoa/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
        {"src": static("homeoa/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
        {
            "src": static("homeoa/icons/icon-maskable-512.png"),
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
    ]
    data = {
        "name": _("HomeOA"),
        "short_name": _("HomeOA"),
        "description": _("Condo association management"),
        "id": "/",
        "start_url": reverse("dashboard:index"),
        "scope": "/",
        "display": "standalone",
        "background_color": "#f5f6f3",
        "theme_color": "#1d6b57",
        "icons": icons,
    }
    response = JsonResponse(data, json_dumps_params={"ensure_ascii": False})
    response["Content-Type"] = "application/manifest+json"
    return response


def service_worker(request):
    """Minimal service worker: an offline page only. It never caches app pages or data."""
    response = render(request, "ui/sw.js", content_type="text/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


def offline(request):
    """Shown by the service worker when a page can't be reached. No user data on it."""
    return render(request, "ui/offline.html")
