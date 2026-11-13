"""Stripe fake plugin is on in the demo seed; its webhook refuses bad signatures.

The pay flows live in test_portal.py.
"""

import json
import re

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers import m2
from helpers.seed import run_script

ASSOC = """
import json
from homeoa.core.models import Association
print("E2EJSON" + json.dumps({"id": Association.objects.order_by("pk").first().pk}))
"""


def test_stripe_enabled_and_webhook_signature(page, seed, login, base_url):
    login(BOARD_EMAIL)
    page.goto("/settings/plugins/")
    expect(page.get_by_role("row", name=re.compile("Stripe", re.I))).to_be_visible()

    assoc = run_script(ASSOC, "association id")["id"]
    body = json.dumps({"id": "evt_e2e_bad", "type": "payment_intent.succeeded"})
    assert m2.post_webhook(base_url, assoc, body, "t=1,v1=bad") == 400
    assert m2.post_webhook(base_url, assoc, body, "") == 400
