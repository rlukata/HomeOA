"""Cloudflare Tunnel mode: client IP from CF-Connecting-IP, compose profile."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from django.test import RequestFactory, override_settings

from homeoa.core.logging import client_ip

ROOT = Path(__file__).resolve().parent.parent
CADDY = "172.31.250.10"
TUNNEL = "172.31.250.11"
VISITOR = "198.51.100.23"


def _req(remote, xff=None, cf=None):
    meta = {"REMOTE_ADDR": remote}
    if xff:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    if cf is not None:
        meta["HTTP_CF_CONNECTING_IP"] = cf
    return RequestFactory().get("/", **meta)


both = override_settings(HOMEOA_TRUSTED_PROXIES=[CADDY], HOMEOA_CF_TUNNEL_PROXIES=[TUNNEL])


@both
def test_direct_peer_ignores_cf_header():
    assert client_ip(_req("203.0.113.9", cf=VISITOR)) == "203.0.113.9"


@both
def test_caddy_only_uses_xff_and_ignores_cf_header():
    assert client_ip(_req(CADDY, "203.0.113.9", cf=VISITOR)) == "203.0.113.9"


@both
def test_caddy_plus_tunnel_uses_cf_header():
    # cloudflared sets XFF to the visitor; Caddy appends the cloudflared IP.
    assert client_ip(_req(CADDY, f"{VISITOR}, {TUNNEL}", cf=VISITOR)) == VISITOR


@both
def test_tunnel_direct_peer_uses_cf_header():
    assert client_ip(_req(TUNNEL, cf=VISITOR)) == VISITOR


@both
def test_spoofed_cf_header_via_caddy_from_untrusted_hop_ignored():
    # Attacker hits Caddy directly with a forged XFF chain ending in a non-tunnel hop.
    req = _req(CADDY, f"{TUNNEL}, 203.0.113.66", cf="1.1.1.1")
    assert client_ip(req) == "203.0.113.66"


@both
def test_bad_cf_header_falls_back_to_tunnel_hop():
    assert client_ip(_req(CADDY, TUNNEL, cf="not-an-ip")) == TUNNEL
    assert client_ip(_req(TUNNEL, cf="")) == TUNNEL
    assert client_ip(_req(TUNNEL)) == TUNNEL


@override_settings(HOMEOA_TRUSTED_PROXIES=[CADDY], HOMEOA_CF_TUNNEL_PROXIES=[])
def test_no_tunnel_configured_cf_header_ignored():
    assert client_ip(_req(CADDY, f"{VISITOR}, {TUNNEL}", cf="1.1.1.1")) == TUNNEL


@override_settings(HOMEOA_TRUSTED_PROXIES=[CADDY], HOMEOA_CF_TUNNEL_PROXIES=["junk", TUNNEL])
def test_invalid_tunnel_setting_entries_skipped():
    assert client_ip(_req(TUNNEL, cf=VISITOR)) == VISITOR


def test_settings_parse(monkeypatch):
    import importlib

    import homeoa.settings.base as base

    monkeypatch.setenv("HOMEOA_CF_TUNNEL_PROXIES", f" {TUNNEL} , ,10.0.0.0/8")
    try:
        assert importlib.reload(base).HOMEOA_CF_TUNNEL_PROXIES == [TUNNEL, "10.0.0.0/8"]
        monkeypatch.delenv("HOMEOA_CF_TUNNEL_PROXIES")
        assert importlib.reload(base).HOMEOA_CF_TUNNEL_PROXIES == []
    finally:
        monkeypatch.delenv("HOMEOA_CF_TUNNEL_PROXIES", raising=False)
        importlib.reload(base)


def _compose_ok():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return r.returncode == 0


@pytest.mark.skipif(not _compose_ok(), reason="docker compose not available")
@pytest.mark.parametrize("profile", [[], ["--profile", "tunnel"]])
def test_compose_config_validates(tmp_path, profile):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("compose.prod.yaml", "Caddyfile", "prod.env.example"):
        shutil.copy(ROOT / "deploy" / name, deploy / name)
    shutil.copy(deploy / "prod.env.example", deploy / "prod.env")
    r = subprocess.run(
        ["docker", "compose", "-f", str(deploy / "compose.prod.yaml"),
         "--env-file", str(deploy / "prod.env"), *profile, "config", "--services"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    services = set(r.stdout.split())
    assert {"db", "web", "worker", "caddy"} <= services
    assert ("cloudflared" in services) == bool(profile)


@pytest.mark.skipif(not _compose_ok(), reason="docker compose not available")
def test_tunnel_token_reaches_only_cloudflared(tmp_path):
    """The token lives in tunnel.env, cloudflared's own env_file, not prod.env."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("compose.prod.yaml", "Caddyfile"):
        shutil.copy(ROOT / "deploy" / name, deploy / name)
    shutil.copy(ROOT / "deploy" / "prod.env.example", deploy / "prod.env")
    (deploy / "tunnel.env").write_text("TUNNEL_TOKEN=sentinel-tunnel-token\n")
    r = subprocess.run(
        ["docker", "compose", "-f", str(deploy / "compose.prod.yaml"),
         "--env-file", str(deploy / "prod.env"), "--profile", "tunnel", "--profile", "backup",
         "config", "--format", "json"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    services = json.loads(r.stdout)["services"]
    assert "sentinel-tunnel-token" in json.dumps(services["cloudflared"])
    for name in ("web", "worker", "backup", "migrate", "caddy"):
        assert "sentinel-tunnel-token" not in json.dumps(services[name]), name
        assert "TUNNEL_TOKEN" not in json.dumps(services[name]), name


@pytest.mark.skipif(not _compose_ok(), reason="docker compose not available")
def test_backup_service_only_with_backup_profile(tmp_path):
    """Production enables backups (COMPOSE_PROFILES=backup); the demo does not."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("compose.prod.yaml", "Caddyfile"):
        shutil.copy(ROOT / "deploy" / name, deploy / name)
    shutil.copy(ROOT / "deploy" / "prod.env.example", deploy / "prod.env")

    def services(*profile):
        r = subprocess.run(
            ["docker", "compose", "-f", str(deploy / "compose.prod.yaml"),
             "--env-file", str(deploy / "prod.env"), *profile, "config", "--services"],
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert r.returncode == 0, r.stderr
        return set(r.stdout.split())

    assert "backup" in services()
    assert "backup" not in services("--profile", "tunnel")
