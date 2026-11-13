from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from . import services


@login_required
def results(request):
    """The search page behind the top bar's search button (``/search/?q=``)."""
    query = services.clean(request.GET.get("q", ""))
    groups = services.search(request.user, request.association, query)
    return render(
        request,
        "search/results.html",
        {
            "q": query,
            "groups": groups,
            "searched": len(query) >= services.MIN_LENGTH,
            "min_length": services.MIN_LENGTH,
        },
    )
