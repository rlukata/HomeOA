"""Content-Security-Policy (report-only switch, nonces, template scan)."""

import re
from pathlib import Path

import pytest
from django.http import HttpResponse
from django.middleware.csp import ContentSecurityPolicyMiddleware
from django.template import engines
from django.test import RequestFactory, override_settings

from homeoa.core.security_headers import content_security_policy, csp_settings

ROOT = Path(__file__).resolve().parent.parent
ENFORCE = "Content-Security-Policy"
REPORT = "Content-Security-Policy-Report-Only"


def _nonce(value: str) -> str:
    return re.search(r"'nonce-([^']+)'", value).group(1)


def test_policy_shape():
    s, r = csp_settings(False)
    assert s == content_security_policy() and r == {}
    s, r = csp_settings(True)
    assert s == {} and r == content_security_policy()
    p = content_security_policy()
    for d in ("script-src", "style-src"):
        assert "'unsafe-inline'" not in p[d] and "'unsafe-eval'" not in p[d]
    assert p["frame-ancestors"] == ["'none'"] and p["object-src"] == ["'none'"]
    assert p["form-action"] == ["'self'"]


def test_base_default_is_report_only(monkeypatch):
    import importlib

    import homeoa.settings.base as base

    monkeypatch.delenv("HOMEOA_CSP_REPORT_ONLY", raising=False)
    mod = importlib.reload(base)
    assert mod.HOMEOA_CSP_REPORT_ONLY is True
    assert mod.SECURE_CSP == {} and mod.SECURE_CSP_REPORT_ONLY
    monkeypatch.setenv("HOMEOA_CSP_REPORT_ONLY", "false")
    mod = importlib.reload(base)
    assert mod.SECURE_CSP and mod.SECURE_CSP_REPORT_ONLY == {}
    monkeypatch.delenv("HOMEOA_CSP_REPORT_ONLY")
    importlib.reload(base)


def test_test_settings_enforce(settings):
    assert settings.HOMEOA_CSP_REPORT_ONLY is False
    assert settings.SECURE_CSP and not settings.SECURE_CSP_REPORT_ONLY


def _view(request):
    tpl = engines["django"].from_string('<style nonce="{{ csp_nonce }}"></style>')
    return HttpResponse(tpl.render({}, request))


def test_fresh_nonce_per_request_and_in_template():
    mw = ContentSecurityPolicyMiddleware(_view)
    nonces = []
    for _ in range(2):
        resp = mw(RequestFactory().get("/"))
        n = _nonce(resp[ENFORCE])
        assert f'nonce="{n}"' in resp.content.decode()
        nonces.append(n)
    assert nonces[0] != nonces[1]


def test_report_only_switch_swaps_header_name():
    enforce, report = csp_settings(True)
    with override_settings(SECURE_CSP=enforce, SECURE_CSP_REPORT_ONLY=report):
        resp = ContentSecurityPolicyMiddleware(_view)(RequestFactory().get("/"))
    assert REPORT in resp and ENFORCE not in resp


@pytest.mark.django_db
def test_csp_on_real_pages(client):
    for path in ("/login/", "/no-such-page/"):
        value = client.get(path)[ENFORCE]
        assert "default-src 'self'" in value and "'unsafe-inline'" not in value


TEMPLATES = sorted(
    [*ROOT.glob("homeoa/**/templates/**/*.html"), *ROOT.glob("plugins/*/*/templates/**/*.html")]
)
# Email bodies are not served under the CSP and need inline styles for mail clients.
EXEMPT = {"notify/email/"}


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p.relative_to(ROOT)))
def test_templates_are_csp_clean(path):
    if any(e in str(path) for e in EXEMPT):
        return
    text = path.read_text()
    problems = []
    for tag in re.findall(r"<(?:script|style)\b[^>]*>", text, re.I):
        if "src=" not in tag and "nonce=" not in tag:
            problems.append(f"inline tag without nonce: {tag}")
    if re.search(r"\son[a-z]+\s*=", text, re.I):
        problems.append("on* handler attribute")
    if re.search(r"\shx-on[:\-=]", text):
        problems.append("hx-on attribute")
    if re.search(r"\sstyle\s*=", text, re.I):
        problems.append("inline style= attribute")
    if "javascript:" in text:
        problems.append("javascript: URL")
    assert not problems, problems


def test_template_scan_finds_templates():
    assert len(TEMPLATES) > 50
