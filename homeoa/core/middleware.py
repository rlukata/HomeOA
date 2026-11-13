from .context import reset_current_association, set_current_association
from .models import Association

# Paths that must work without touching association data (e.g. when the DB is down).
EXEMPT_PREFIXES = ("/healthz", "/static/")


class CurrentAssociationMiddleware:
    """Resolve the association for this request.

    The UI assumes a single association: we take the first one. Multi-association
    routing (by host or path) can replace ``resolve`` later without touching models.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def resolve(self, request):
        return Association.objects.order_by("pk").first()

    def __call__(self, request):
        if request.path.startswith(EXEMPT_PREFIXES):
            request.association = None
            return self.get_response(request)
        association = self.resolve(request)
        request.association = association
        token = set_current_association(association)
        try:
            return self.get_response(request)
        finally:
            reset_current_association(token)
