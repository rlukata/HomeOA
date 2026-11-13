from django.shortcuts import redirect
from django.urls import reverse

from .views import ENROL_REQUIRED


class MFAEnrolmentMiddleware:
    """Keep users who must enrol in MFA on the enrolment pages (TOTP or passkey) until they do."""

    ALLOWED = ("/static/", "/healthz", "/logout/", "/account/passkeys/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and request.session.get(ENROL_REQUIRED)
            and not request.path.startswith(self.ALLOWED)
        ):
            target = reverse("accounts:mfa_setup")
            if request.path != target:
                return redirect(target)
        return self.get_response(request)
