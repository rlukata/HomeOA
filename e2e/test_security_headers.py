"""Permissions-Policy is sent on pages and keeps passkeys on our origin.

HSTS is prod-only (HTTPS) and is covered by unit tests; the passkey flows under
this header are covered by test_passkeys.py.
"""

import urllib.request


def test_permissions_policy_header(live_server_url):
    with urllib.request.urlopen(f"{live_server_url}/login/") as resp:
        policy = resp.headers.get("Permissions-Policy", "")
    assert "camera=()" in policy
    assert "publickey-credentials-get=(self)" in policy
    assert "publickey-credentials-create=(self)" in policy
