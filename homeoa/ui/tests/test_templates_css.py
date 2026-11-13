"""Guard against layout regressions in templates."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT.parent


def _templates():
    """Core templates plus every plugin package's templates (plugins/*/)."""
    yield from ROOT.rglob("templates/**/*.html")
    yield from (REPO / "plugins").glob("*/**/templates/**/*.html")


def test_card_gap_only_on_empty_spacers():
    """.card-gap fixes height to 10px; on a card it makes content overlap the next card."""
    offenders = []
    for path in _templates():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in re.finditer(r'class="([^"]*)"', line):
                classes = m.group(1).split()
                if "card-gap" in classes and "card" in classes:
                    offenders.append(f"{path.relative_to(REPO)}:{n}")
    assert not offenders, offenders


def test_guard_scans_plugin_templates():
    paths = {p.relative_to(REPO).parts[0] for p in _templates()}
    assert {"homeoa", "plugins"} <= paths


# Templates still allowed to carry style= attributes, as {path: max count}.
# This list may only shrink: fix a file, then lower or drop its entry.
STYLE_ALLOWLIST = {
    # HTML email: mail clients ignore linked stylesheets, so inline styles are required.
    "homeoa/notify/templates/notify/email/message.html": 5,
}

STYLE_ATTR = re.compile(r"\sstyle\s*=")


def _style_counts():
    counts = {}
    for path in _templates():
        n = len(STYLE_ATTR.findall(path.read_text()))
        if n:
            counts[path.relative_to(REPO).as_posix()] = n
    return counts


def test_no_inline_styles_outside_allowlist():
    """Use base.css classes instead of style=; the allowlist may only shrink."""
    over = {p: n for p, n in _style_counts().items() if n > STYLE_ALLOWLIST.get(p, 0)}
    assert not over, f"inline style= found (use base.css classes): {over}"


def test_style_allowlist_has_no_slack():
    """Lower the allowlist when a file is cleaned, so it cannot grow back."""
    counts = _style_counts()
    stale = {p: (n, counts.get(p, 0)) for p, n in STYLE_ALLOWLIST.items() if counts.get(p, 0) < n}
    assert not stale, f"shrink STYLE_ALLOWLIST (allowed, actual): {stale}"


def test_style_regex_matches_attribute_only():
    assert STYLE_ATTR.search('<p style="x">')
    assert STYLE_ATTR.search("<p\n  style='x'>")
    assert not STYLE_ATTR.search('<p class="a" data-style="x">')
