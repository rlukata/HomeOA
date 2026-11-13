"""Prod settings, JSON logs with request id, trusted-proxy client IP, base URL check,
and (opt-in) the backup -> restore drill."""

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import RequestFactory, override_settings

from homeoa.audit.context import RequestIdLogFilter, audit_context
from homeoa.core.checks import check_base_url
from homeoa.core.logging import JsonFormatter, client_ip, logging_config

ROOT = Path(__file__).resolve().parent.parent

PROD_ENV = {
    "DJANGO_SETTINGS_MODULE": "homeoa.settings.prod",
    "HOMEOA_SECRET_KEY": "x" * 60 + "-dummy-ci-secret",
    "HOMEOA_FIELD_KEYS": "ZxBFQzV0wh4mnXQim1_pLdCRfc-HXr6s8f-IZO3m-ow=",
    "HOMEOA_ALLOWED_HOSTS": "hoa.example.org",
    "HOMEOA_BASE_URL": "https://hoa.example.org",
    "BACKUP_AGE_RECIPIENTS": "age1treasurer age1president",
}


def _manage(*args, env_overrides=None):
    env = {**os.environ, **PROD_ENV, **(env_overrides or {})}
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_prod_check_deploy_passes_with_dummy_env():
    result = _manage("check", "--deploy", "--fail-level", "WARNING")
    assert result.returncode == 0, result.stderr + result.stdout


def test_prod_requires_https_base_url():
    result = _manage("check", env_overrides={"HOMEOA_BASE_URL": "http://hoa.example.org"})
    assert result.returncode != 0
    assert "homeoa.E001" in result.stderr


@pytest.mark.parametrize(
    ("url", "ok"),
    [("", False), ("http://a.org", False), ("https://", False), ("https://a.org", True)],
)
def test_base_url_check(url, ok):
    with override_settings(HOMEOA_BASE_URL=url, HOMEOA_BASE_URL_REQUIRED=True):
        assert (check_base_url() == []) is ok


def test_base_url_optional_outside_prod():
    with override_settings(HOMEOA_BASE_URL="", HOMEOA_BASE_URL_REQUIRED=False):
        assert check_base_url() == []


def _format(msg="hello", **extra):
    record = logging.makeLogRecord({"name": "homeoa.x", "levelname": "INFO", "msg": msg, **extra})
    RequestIdLogFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


def test_json_log_carries_request_id_and_extras():
    with audit_context(request_id="req-123"):
        out = _format(action="post_entry", amount="10.00")
    assert out["message"] == "hello"
    assert out["request_id"] == "req-123"
    assert out["level"] == "INFO"
    assert out["action"] == "post_entry"
    assert out["amount"] == "10.00"


def test_json_log_outside_request():
    assert _format()["request_id"] == "-"


def test_logging_config_wires_filter_everywhere():
    cfg = logging_config(mail_admins=True)
    for handler in cfg["handlers"].values():
        assert "request_id" in handler["filters"]
    assert "mail_admins" in cfg["loggers"]["django"]["handlers"]
    assert "mail_admins" not in logging_config()["handlers"]


def _req(remote, xff=None):
    meta = {"REMOTE_ADDR": remote}
    if xff:
        meta["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **meta)


@override_settings(HOMEOA_TRUSTED_PROXIES=[])
def test_xff_ignored_without_trusted_proxies():
    assert client_ip(_req("10.0.0.5", "1.2.3.4")) == "10.0.0.5"


@override_settings(HOMEOA_TRUSTED_PROXIES=["172.31.250.10"])
def test_xff_ignored_from_untrusted_peer():
    assert client_ip(_req("8.8.8.8", "1.2.3.4")) == "8.8.8.8"


@override_settings(HOMEOA_TRUSTED_PROXIES=["172.31.250.10"])
def test_xff_used_from_trusted_proxy_rightmost_untrusted_hop():
    # A client-supplied spoofed left entry is ignored; Caddy appended the real peer.
    assert client_ip(_req("172.31.250.10", "9.9.9.9, 203.0.113.7")) == "203.0.113.7"


@override_settings(HOMEOA_TRUSTED_PROXIES=["172.31.250.0/24"])
def test_xff_cidr_and_garbage():
    assert client_ip(_req("172.31.250.3", "203.0.113.7, 172.31.250.9")) == "203.0.113.7"
    assert client_ip(_req("172.31.250.3", "not-an-ip")) == "172.31.250.3"
    assert client_ip(_req("172.31.250.3")) == "172.31.250.3"


@pytest.mark.skipif(
    os.environ.get("HOMEOA_DEPLOY_DRILL") != "1" or not shutil.which("docker"),
    reason="set HOMEOA_DEPLOY_DRILL=1 (needs Docker) to run the backup/restore drill",
)
def test_backup_restore_drill():
    result = subprocess.run(
        [str(ROOT / "deploy" / "drill.sh")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "DRILL PASSED" in result.stdout


def test_no_profile_flag_in_deploy_commands():
    """--profile replaces COMPOSE_PROFILES from prod.env; profiles must come from prod.env."""
    root = Path(__file__).resolve().parent.parent / "deploy"
    for name in ("RUNBOOK.md",):
        for n, line in enumerate((root / name).read_text().splitlines(), 1):
            if "--profile" in line:
                assert "compose" not in line and "up -d" not in line, f"{name}:{n}: {line}"
