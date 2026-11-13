"""Login helpers. The full magic-link flow is tested in test_auth.py; other
scenarios sign in with a link minted by the test-only command
`e2e_login_url <email>`, which uses the real token service.

Opening the link (GET) shows a "Sign in as <masked email>" page; only the
button (POST) consumes the token."""

from __future__ import annotations

import re

SIGN_IN_BUTTON = re.compile(r"^sign in", re.IGNORECASE)


def open_link(page, url: str) -> None:
    """GET the link, then click the sign-in button if the confirm page shows.
    An invalid link renders an error page with no button."""
    page.goto(url)
    button = page.get_by_role("button", name=SIGN_IN_BUTTON)
    if button.count():
        button.first.click()
        page.wait_for_load_state()


def login_as(page, base_url: str, email: str) -> None:
    from conftest import manage

    url = manage("e2e_login_url", email).stdout.strip().splitlines()[-1]
    if url.startswith("/"):
        url = base_url + url
    open_link(page, url)
