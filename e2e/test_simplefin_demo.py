"""Under E2E settings the seeded demo's SimpleFIN link is the fake bridge,
never the real demo bridge, and the bank feed page loads."""

from urllib.parse import urlsplit

from playwright.sync_api import expect

from conftest import BOARD_EMAIL
from helpers.seed import run_script

HOST = """
import json
from homeoa.core.models import Association
from homeoa.plugins.services import installation_for
from homeoa_simplefin.sync import PLUGIN_ID
inst = installation_for(Association.objects.order_by("pk").first(), PLUGIN_ID)
print("E2EJSON" + json.dumps({"url": (inst.secrets or {}).get("access_url") if inst else None}))
"""


def test_demo_simplefin_is_fake_bridge(page, seed, login):
    url = run_script(HOST, "simplefin access url")["url"]
    assert url, "demo seed has no SimpleFIN connection"
    assert urlsplit(url).hostname == "bridge.simplefin.fake"
    login(BOARD_EMAIL)
    resp = page.goto("/money/bank/")
    assert resp.ok, resp.status
    expect(page.get_by_role("heading").first).to_be_visible()
