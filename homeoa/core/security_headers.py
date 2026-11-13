"""Content-Security-Policy and Permissions-Policy.

The CSP itself is sent by Django's built-in ``ContentSecurityPolicyMiddleware`` from
``SECURE_CSP`` / ``SECURE_CSP_REPORT_ONLY`` (built by :func:`csp_settings`); templates
put ``nonce="{{ csp_nonce }}"`` on any inline ``<style>`` block. Scripts are static
files only: no inline scripts, no ``on*=`` handlers, no ``hx-on``, no eval (htmx runs
with ``allowEval: false`` and ``includeIndicatorStyles: false``).

Headers live in Django, not Caddy, so dev, E2E and production send the same policy.
"""

from __future__ import annotations

from django.utils.csp import CSP

# Stripe.js for bank linking (homeoa-stripe ``bank.html``): its script, the Financial
# Connections iframes, and the API it calls from the browser. Nothing else external.
STRIPE_SCRIPT = ("https://js.stripe.com", "https://*.js.stripe.com")
STRIPE_FRAME = ("https://js.stripe.com", "https://*.js.stripe.com", "https://hooks.stripe.com")
STRIPE_CONNECT = ("https://api.stripe.com",)


def content_security_policy() -> dict:
    return {
        "default-src": [CSP.SELF],
        "script-src": [CSP.SELF, CSP.NONCE, *STRIPE_SCRIPT],
        "style-src": [CSP.SELF, CSP.NONCE],
        "img-src": [CSP.SELF, "data:"],
        "font-src": [CSP.SELF],
        "connect-src": [CSP.SELF, *STRIPE_CONNECT],
        # 'self' for the bill-attachment PDF preview (ap/detail.html).
        "frame-src": [CSP.SELF, *STRIPE_FRAME],
        "frame-ancestors": [CSP.NONE],
        "base-uri": [CSP.SELF],
        "form-action": [CSP.SELF],
        "object-src": [CSP.NONE],
        "manifest-src": [CSP.SELF],
        "worker-src": [CSP.SELF],
    }


def csp_settings(report_only: bool) -> tuple[dict, dict]:
    """(SECURE_CSP, SECURE_CSP_REPORT_ONLY): exactly one of them carries the policy."""
    policy = content_security_policy()
    return ({}, policy) if report_only else (policy, {})


PERMISSIONS_POLICY = ", ".join(
    [
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "payment=()",
        "usb=()",
        "interest-cohort=()",
        # Passkeys stay allowed on our own origin.
        "publickey-credentials-get=(self)",
        "publickey-credentials-create=(self)",
    ]
)


class PermissionsPolicyMiddleware:
    """Adds ``Permissions-Policy`` unless the view set its own."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = PERMISSIONS_POLICY
        return response
