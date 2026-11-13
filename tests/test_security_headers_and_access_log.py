"""Access log with the real client IP, Permissions-Policy, HSTS, prod.env."""

import json
import logging
import shutil
import subprocess

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings

from homeoa.core.access_log import AccessLogMiddleware
from homeoa.core.security_headers import PERMISSIONS_POLICY, PermissionsPolicyMiddleware

from .test_deploy import ROOT, _manage

CADDY = "172.31.250.10"
TUNNEL = "172.31.250.11"
VISITOR = "198.51.100.23"


def _req(remote, xff=None, cf=None, path="/home/"):
    meta = {"REMOTE_ADDR": remote}
    if xff:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    if cf is not None:
        meta["HTTP_CF_CONNECTING_IP"] = cf
    return RequestFactory().get(path, **meta)


def _logged(caplog, request, status=200):
    mw = AccessLogMiddleware(lambda r: HttpResponse(status=status))
    with caplog.at_level(logging.INFO, logger="homeoa.access"):
        caplog.clear()
        mw(request)
    return [r for r in caplog.records if r.name == "homeoa.access"]


both = override_settings(HOMEOA_TRUSTED_PROXIES=[CADDY], HOMEOA_CF_TUNNEL_PROXIES=[TUNNEL])
caddy_only = override_settings(HOMEOA_TRUSTED_PROXIES=[CADDY], HOMEOA_CF_TUNNEL_PROXIES=[])


@pytest.mark.parametrize(
    ("cfg", "req", "expected"),
    [
        # tunnel mode: cloudflared -> Caddy -> web
        (both, _req(CADDY, f"{VISITOR}, {TUNNEL}", cf=VISITOR), VISITOR),
        # caddy mode: XFF-derived visitor, CF header ignored
        (caddy_only, _req(CADDY, "203.0.113.9", cf=VISITOR), "203.0.113.9"),
        # spoofed CF-Connecting-IP via Caddy from an untrusted hop
        (both, _req(CADDY, "203.0.113.66", cf="1.1.1.1"), "203.0.113.66"),
        # spoofed headers straight to web
        (both, _req("203.0.113.5", "1.2.3.4", cf="1.1.1.1"), "203.0.113.5"),
    ],
)
def test_access_log_uses_client_ip(caplog, cfg, req, expected):
    with cfg:
        [rec] = _logged(caplog, req, status=302)
    assert rec.client_ip == expected
    assert rec.status == 302 and rec.method == "GET" and rec.path == "/home/"
    assert rec.getMessage().startswith(f'{expected} "GET /home/" 302')


def test_access_log_skips_healthz(caplog):
    assert _logged(caplog, _req("10.0.0.1", path="/healthz")) == []


def test_permissions_policy_header():
    mw = PermissionsPolicyMiddleware(lambda r: HttpResponse())
    value = mw(RequestFactory().get("/"))["Permissions-Policy"]
    assert value == PERMISSIONS_POLICY
    for item in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
        assert item in value
    assert "publickey-credentials-get=(self)" in value
    assert "publickey-credentials-create=(self)" in value


def test_permissions_policy_view_override_kept():
    def view(r):
        resp = HttpResponse()
        resp["Permissions-Policy"] = "camera=(self)"
        return resp

    assert PermissionsPolicyMiddleware(view)(RequestFactory().get("/"))["Permissions-Policy"] == (
        "camera=(self)"
    )


@pytest.mark.django_db
def test_permissions_policy_on_real_pages(client):
    assert client.get("/login/")["Permissions-Policy"] == PERMISSIONS_POLICY
    assert client.get("/no-such-page/")["Permissions-Policy"] == PERMISSIONS_POLICY


def test_prod_settings_hsts_and_access_log():
    code = (
        "from django.conf import settings as s; import json; print(json.dumps(["
        "s.SECURE_HSTS_SECONDS, s.SECURE_HSTS_INCLUDE_SUBDOMAINS, s.SECURE_HSTS_PRELOAD,"
        "s.MIDDLEWARE[0]]))"
    )
    result = _manage("shell", "-c", code)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [
        31536000,
        False,
        False,
        "homeoa.core.access_log.AccessLogMiddleware",
    ]


def test_gunicorn_access_log_off():
    text = (ROOT / "deploy" / "entrypoint.sh").read_text()
    assert "--access-logfile -" not in text


# ------------------------------------------------------------------ Caddyfile


def test_caddyfile_takes_cf_header_only_from_tunnel():
    text = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "client_ip_headers CF-Connecting-IP" in text
    assert "trusted_proxies static {$HOMEOA_CF_TUNNEL_IP:172.31.250.11}/32" in text


@pytest.mark.parametrize("mode", ["caddy", "tunnel"])
def test_caddyfile_validates(mode):
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    image = "caddy:2"
    compose = (ROOT / "deploy" / "compose.prod.yaml").read_text()
    for line in compose.splitlines():
        if "image: caddy" in line:
            image = line.split("image:", 1)[1].strip()
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-e", f"HOMEOA_TLS_MODE={mode}",
            "-v", f"{ROOT / 'deploy' / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            image, "caddy", "validate", "--config", "/etc/caddy/Caddyfile",
        ],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    if result.returncode != 0 and "Cannot connect" in result.stderr:
        pytest.skip("docker daemon not running")
    assert result.returncode == 0, result.stderr[-2000:]
