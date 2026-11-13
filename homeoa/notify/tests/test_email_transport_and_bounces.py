"""Transport failures vs real bounces, Resend/Svix bounces, SMTP SSL, sender name."""

# ruff: noqa: F811  (fixtures imported from test_email)
import base64
import importlib
import json
import smtplib
import sys
import time

import pytest
from cryptography.fernet import Fernet
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from homeoa.notify import bounces, consent
from homeoa.notify.models import Delivery, Notification
from homeoa.plugins import services as plugin_services
from homeoa.registry import services as registry_services

from .test_email import (  # noqa: F401  (fixtures)
    AS_OF,
    assoc,
    board_person,
    board_user,
    registered,
    send,
    unit,
)

WHSEC = "whsec_" + base64.b64encode(b"resend-test-signing-key-32bytes!").decode()
F, B, S = Delivery.Status.FAILED, Delivery.Status.BOUNCED, Delivery.Status.SENT


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "transport"),
        (smtplib.SMTPServerDisconnected("gone"), "transport"),
        (ConnectionRefusedError("refused"), "transport"),
        (TimeoutError("timed out"), "transport"),
        (smtplib.SMTPRecipientsRefused({"x@example.com": (550, b"no such user")}), "recipient"),
    ],
)
def test_channel_classifies_send_errors(assoc, board_person, monkeypatch, exc, kind):
    def boom(self, *a, **k):
        raise exc

    monkeypatch.setattr(EmailMultiAlternatives, "send", boom)
    [d] = send(assoc, [board_person])
    assert d.status == F
    assert d.failure_kind == kind


def test_unexpected_exception_is_transport(assoc, board_person, monkeypatch):
    from homeoa.providers.defaults.smtp import SmtpEmail

    def boom(self, m):
        raise RuntimeError("provider outage")

    monkeypatch.setattr(SmtpEmail, "send", boom)
    [d] = send(assoc, [board_person])
    assert d.status == F and d.failure_kind == "transport"


# ------------------------------------------------------------------ consent counting


def outcome(assoc, person, n, status, kind=""):
    note = Notification.objects.create(
        association=assoc,
        type_key="test.x",
        recipient=person,
        idempotency_key=f"k-{n}",
        as_of=AS_OF,
    )
    now = timezone.now()
    d = Delivery.objects.create(
        association=assoc,
        notification=note,
        channel="email",
        status=status,
        failure_kind=kind,
        failed_at=now if status == F else None,
        bounced_at=now if status == B else None,
        sent_at=now if status == S else None,
    )
    consent.after_email_outcome(d)
    return d


def consent_ok(person):
    return registry_services.current_notice_preference(person).valid_consent


def test_two_transport_failures_keep_consent(assoc, board_person):
    outcome(assoc, board_person, 1, F, "transport")
    outcome(assoc, board_person, 2, F, "transport")
    assert consent_ok(board_person)


def test_two_bounces_revoke(assoc, board_person):
    outcome(assoc, board_person, 1, B)
    assert consent_ok(board_person)
    outcome(assoc, board_person, 2, B)
    assert not consent_ok(board_person)


def test_bounce_transport_bounce_revokes(assoc, board_person):
    outcome(assoc, board_person, 1, B)
    outcome(assoc, board_person, 2, F, "transport")
    assert consent_ok(board_person)
    outcome(assoc, board_person, 3, B)
    assert not consent_ok(board_person)


def test_recipient_refusal_counts(assoc, board_person):
    outcome(assoc, board_person, 1, F, "recipient")
    outcome(assoc, board_person, 2, B)
    assert not consent_ok(board_person)


def test_success_breaks_run(assoc, board_person):
    outcome(assoc, board_person, 1, B)
    outcome(assoc, board_person, 2, S)
    outcome(assoc, board_person, 3, B)
    assert consent_ok(board_person)


def test_smtp_auth_failures_keep_consent(assoc, board_person, monkeypatch):
    def boom(self, *a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(EmailMultiAlternatives, "send", boom)
    [d] = send(assoc, [board_person])
    outcome(assoc, board_person, 9, F, "transport")
    assert d.status == F
    assert consent_ok(board_person)


# ------------------------------------------------------------------ sender name and reply-to


def test_sender_is_association_name_and_reply_to(assoc, board_person, settings):
    settings.DEFAULT_FROM_EMAIL = "notices@example.org"
    settings.HOMEOA_EMAIL_REPLY_TO = "board.member@example.net"
    send(assoc, [board_person])
    [msg] = mail.outbox
    assert msg.from_email == '"Cedar Row" <notices@example.org>' or msg.from_email == (
        "Cedar Row <notices@example.org>"
    )
    assert msg.reply_to == ["board.member@example.net"]


def test_no_reply_to_by_default(assoc, board_person, settings):
    settings.HOMEOA_EMAIL_REPLY_TO = ""
    send(assoc, [board_person])
    assert mail.outbox[0].reply_to == []


# ------------------------------------------------------------------ Svix signature


def headers(body, secret=WHSEC, msg_id="msg_1", ts=None):
    ts = int(time.time()) if ts is None else ts
    return {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": bounces.svix_sign(body, secret, msg_id, ts),
    }


def test_svix_valid():
    body = b'{"type":"email.bounced"}'
    assert bounces.svix_verify(headers(body), body, WHSEC)


def test_svix_wrong_secret():
    body = b"{}"
    other = "whsec_" + base64.b64encode(b"another-key-another-key-another!").decode()
    assert not bounces.svix_verify(headers(body, secret=other), body, WHSEC)


def test_svix_tampered_body():
    assert not bounces.svix_verify(headers(b"{}"), b'{"x":1}', WHSEC)


def test_svix_old_timestamp():
    body = b"{}"
    assert not bounces.svix_verify(headers(body, ts=int(time.time()) - 600), body, WHSEC)
    assert not bounces.svix_verify(headers(body, ts=int(time.time()) + 600), body, WHSEC)


def test_svix_multiple_entries_any_match():
    body = b"{}"
    h = headers(body)
    h["svix-signature"] = "v1,AAAA v2,zzz " + h["svix-signature"]
    assert bounces.svix_verify(h, body, WHSEC)
    h["svix-signature"] = "v1,AAAA v1,BBBB"
    assert not bounces.svix_verify(h, body, WHSEC)


def test_svix_missing_headers():
    assert not bounces.svix_verify({}, b"{}", WHSEC)


# ------------------------------------------------------------------ Resend webhook


def enable_resend(assoc):
    plugin_services.save_installation(
        assoc, bounces.PLUGIN_ID, enabled=True, data={"secret": WHSEC, "format": "resend"}
    )


def resend_body(email_id="", message_id="", type_="email.bounced"):
    data = {"email_id": email_id, "bounce": {"message": "550 5.1.1 user unknown"}}
    if message_id:
        data["headers"] = [{"name": "Message-ID", "value": message_id}]
    return json.dumps({"type": type_, "data": data}).encode()


def post(client, assoc, body, h):
    return client.post(
        f"/hooks/{bounces.PLUGIN_ID}/{assoc.pk}/",
        data=body,
        content_type="application/json",
        headers=h,
    )


def test_resend_bounce_by_email_id(client, assoc, board_person):
    enable_resend(assoc)
    [d] = send(assoc, [board_person])
    body = resend_body(email_id=d.provider_id)
    h = headers(body, msg_id="msg_a")
    assert post(client, assoc, body, h).status_code == 200
    d.refresh_from_db()
    assert d.status == B and d.error == "550 5.1.1 user unknown"
    assert post(client, assoc, body, h).content == b"duplicate"


def test_resend_bounce_by_message_id_header(client, assoc, board_person):
    enable_resend(assoc)
    [d] = send(assoc, [board_person])
    body = resend_body(email_id="4ef9a417-resend-id", message_id=d.provider_id)
    assert post(client, assoc, body, headers(body, msg_id="msg_b")).status_code == 200
    d.refresh_from_db()
    assert d.status == B


@pytest.mark.parametrize("bad", ["wrong", "stale", "missing"])
def test_resend_bad_signature_rejected(client, assoc, board_person, bad):
    enable_resend(assoc)
    [d] = send(assoc, [board_person])
    body = resend_body(email_id=d.provider_id)
    if bad == "wrong":
        h = headers(body, secret="whsec_" + base64.b64encode(b"x" * 32).decode())
    elif bad == "stale":
        h = headers(body, ts=int(time.time()) - 3600)
    else:
        h = {}
    assert post(client, assoc, body, h).status_code == 400
    d.refresh_from_db()
    assert d.status == S


def test_resend_other_event_ignored(client, assoc, board_person):
    enable_resend(assoc)
    [d] = send(assoc, [board_person])
    body = resend_body(email_id=d.provider_id, type_="email.delivered")
    assert post(client, assoc, body, headers(body, msg_id="msg_c")).status_code == 200
    d.refresh_from_db()
    assert d.status == S


# ------------------------------------------------------------------ settings


def _reload_prod(monkeypatch, **env):
    import homeoa.settings.base as base

    monkeypatch.setattr(base, "SECRET_KEY", "prod-secret")
    monkeypatch.setattr(base, "HOMEOA_FIELD_KEYS", [Fernet.generate_key().decode()])
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("homeoa.settings.prod", None)
    try:
        return importlib.import_module("homeoa.settings.prod")
    finally:
        sys.modules.pop("homeoa.settings.prod", None)


def test_prod_ssl_forces_tls_off(monkeypatch):
    s = _reload_prod(
        monkeypatch, HOMEOA_EMAIL_USE_SSL="1", HOMEOA_EMAIL_USE_TLS="1", HOMEOA_EMAIL_PORT="465"
    )
    assert s.EMAIL_USE_SSL is True and s.EMAIL_USE_TLS is False


def test_prod_default_tls(monkeypatch):
    monkeypatch.delenv("HOMEOA_EMAIL_USE_SSL", raising=False)
    monkeypatch.delenv("HOMEOA_EMAIL_USE_TLS", raising=False)
    s = _reload_prod(monkeypatch)
    assert s.EMAIL_USE_SSL is False and s.EMAIL_USE_TLS is True
