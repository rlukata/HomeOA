"""axe-core scan. Loads axe from $HOMEOA_E2E_AXE_JS (a local file) or the pinned
cdnjs build. Fails on 'serious' or 'critical' violations."""

from __future__ import annotations

import functools
import os
import urllib.request
from pathlib import Path

AXE_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"


@functools.cache
def _axe_source() -> str:
    if path := os.environ.get("HOMEOA_E2E_AXE_JS"):
        return Path(path).read_text()
    with urllib.request.urlopen(AXE_URL) as resp:
        return resp.read().decode()


def assert_no_serious_violations(page, tags=("wcag2a", "wcag2aa", "wcag21aa")) -> None:
    # Injected with evaluate(), not a <script> tag, so the enforced CSP
    # does not block it and no violation is reported.
    page.evaluate(_axe_source())
    result = page.evaluate(
        "tags => axe.run(document, {runOnly: {type: 'tag', values: tags}})", list(tags)
    )
    if os.environ.get("HOMEOA_E2E_AXE_LOG"):
        for v in result["violations"]:
            print("AXE", page.url, v["id"], v["impact"], [n["target"] for n in v["nodes"]][:3])
    bad = [v for v in result["violations"] if v["impact"] in ("serious", "critical")]
    assert not bad, f"axe on {page.url}: " + "; ".join(
        f"{v['id']} ({v['impact']}) x{len(v['nodes'])} e.g. {v['nodes'][0]['target']}: "
        f"{(v['nodes'][0].get('any') or [{}])[0].get('message', '')}"
        for v in bad
    )
