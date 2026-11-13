from .nav import get_hubs, hidden_subtab_urls


def _current(request, hubs):
    """The hub and section tabs for the page being shown, worked out from its address.

    A page belongs to the section whose URL is the longest prefix of the page's path, so
    detail pages (``/votes/3/``) light up their section (``/votes/``). Views that build
    their own ``current_hub`` and ``subtabs`` override these defaults.
    """
    path = request.path
    best = None
    for hub in hubs:
        for i in range(len(hub.subtabs)):
            url = hub.subtab_url(i)
            inside = url != "/" and (path == url or path.startswith(url.rstrip("/") + "/"))
            if inside and (best is None or len(url) > len(best[2])):
                best = (hub, i, url)
    if best is None:
        return None, []
    hub, index, _url = best
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": i == index}
        for i, t in enumerate(hub.subtabs)
    ]
    return hub, tabs


def shell(request):
    """Navigation data for the board shell (hubs, tab bar, More sheet, section tabs)."""
    hubs = get_hubs(request)
    current_hub, subtabs = _current(request, hubs)
    return {
        "nav_hubs": hubs,
        "nav_tabbar": [h for h in hubs if h.tabbar],
        "nav_more": [h for h in hubs if h.overflow],
        "nav_sheet": [h for h in hubs if not h.tabbar],
        "nav_hidden_urls": hidden_subtab_urls(request),
        "association": getattr(request, "association", None),
        "current_hub": current_hub,
        "subtabs": subtabs,
    }
