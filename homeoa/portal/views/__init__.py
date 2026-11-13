"""The resident app shell under ``/me/`` (POR-01).

Every view is a ``resident_view``: while residents are off it shows the "not open yet"
page.
"""

from homeoa.access.decorators import resident_view

from ..pay import pay, pay_confirm
from . import profile as profile_views
from .base import _render
from .history import payments
from .home import home
from .statements import statement_pdf, statements


@resident_view
def requests_tab(request):
    from ..requests import for_user

    return _render(request, "requests", "portal/requests.html", **for_user(request))


from homeoa.documents.library_views import portal_docs as docs  # noqa: E402


@resident_view
def more(request):
    return _render(request, "more", "portal/more.html")


__all__ = [
    "pay_confirm",
    "docs",
    "home",
    "more",
    "pay",
    "payments",
    "profile_views",
    "requests_tab",
    "statement_pdf",
    "statements",
]
