"""Hides the pages of optional modules that are off for the request's association."""

from django.http import Http404

from .modules import page_on


class ModulePagesMiddleware:
    """A page that belongs to a switched-off module returns 404, whatever the method (so
    htmx partials and form posts are refused too). Listed after the current-association
    middleware, which sets ``request.association``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        match = getattr(request, "resolver_match", None)
        association = getattr(request, "association", None)
        if match is None or association is None:
            return None
        if not page_on(association, match.view_name):
            raise Http404
        return None
