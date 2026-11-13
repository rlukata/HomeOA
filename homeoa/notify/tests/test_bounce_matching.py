"""The relay's message id from the SMTP DATA reply, and bounce matching."""

# ruff: noqa: F811  (fixtures imported from test_email)
import smtplib

import pytest

from homeoa.notify import bounces
from homeoa.notify.models import Delivery
from homeoa.providers.defaults import smtp as smtp_provider
from homeoa.providers.defaults.smtp import reply_id
from homeoa.providers.email import EmailRequest

from .test_email import (  # noqa: F401  (fixtures)
    AS_OF,
    assoc,
    board_person,
    board_user,
    registered,
    send,
    unit,
)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (b"<abc123>", "abc123"),
        (b"2.0.0 <4ef9-a1b2-c3d4>", "4ef9-a1b2-c3d4"),
        (b"abc12345", "abc12345"),
        (b"49a3999e-3fa5-4e14-a1e9-8a2b9c3d4e5f", "49a3999e-3fa5-4e14-a1e9-8a2b9c3d4e5f"),
        (b"OK", ""),
        (b"2.0.0 Ok: queued as 4ABC123", ""),
        (b"Message accepted for delivery", ""),
        (b"queued", ""),
        (b"<a> <b>", ""),
        (b"", ""),
    ],
)
def test_reply_id_rule(reply, expected):
    assert reply_id(reply) == expected


class FakeSMTP:
    """Stub smtplib.SMTP / SMTP_SSL: sendmail ends with data(), as smtplib's does."""

    reply = b"OK"
    fail = None
    instances = []

    def __init__(self, host="", port=0, *a, **k):
        self.sent = []
        FakeSMTP.instances.append(self)

    def starttls(self, *a, **k):
        return (220, b"ready")

    def login(self, user, password):
        return (235, b"ok")

    def sendmail(self, from_addr, to_addrs, msg, *a, **k):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.data(msg)
        self.sent.append(msg)
        return {}

    def data(self, msg):
        return (250, FakeSMTP.reply)

    def quit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_smtp(settings, monkeypatch):
    import django.core.mail.backends.smtp as backend

    settings.EMAIL_BACKEND = smtp_provider.DJANGO_SMTP
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""
    monkeypatch.setattr(backend.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(backend.smtplib, "SMTP_SSL", FakeSMTP)
    FakeSMTP.reply, FakeSMTP.fail, FakeSMTP.instances = b"OK", None, []
    return FakeSMTP


def _send_one():
    return smtp_provider.SmtpEmail().send(
        EmailRequest(to=("a@example.org",), subject="Hi", text="Body")
    )


@pytest.mark.parametrize("ssl", [False, True])
def test_provider_returns_reply_id(fake_smtp, settings, ssl):
    settings.EMAIL_USE_SSL, settings.EMAIL_USE_TLS = ssl, not ssl
    fake_smtp.reply = b"<abc123>"
    res = _send_one()
    assert res.sent and res.provider_id == "abc123"
    assert res.message_id.startswith("<") and res.message_id != res.provider_id
    assert fake_smtp.instances[0].sent


def test_provider_bare_and_generic_replies(fake_smtp):
    fake_smtp.reply = b"abc12345"
    assert _send_one().provider_id == "abc12345"
    fake_smtp.reply = b"OK"
    res = _send_one()
    assert res.sent and res.provider_id == "" and res.message_id


def test_provider_failure_keeps_classification(fake_smtp):
    fake_smtp.fail = smtplib.SMTPRecipientsRefused({"a@example.org": (550, b"no")})
    res = _send_one()
    assert not res.sent and res.failure_kind == "recipient" and res.provider_id == ""
    fake_smtp.fail = smtplib.SMTPServerDisconnected("gone")
    assert _send_one().failure_kind == "transport"


def test_non_smtp_backend_unchanged(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    res = _send_one()
    assert res.sent and res.message_id and res.provider_id == ""


def _resend_bounce(email_id="", message_id=""):
    data = {"bounce": {"message": "550 mailbox unavailable"}}
    if email_id:
        data["email_id"] = email_id
    if message_id:
        data["headers"] = [{"name": "Message-ID", "value": message_id}]
    return {"type": "email.bounced", "data": data}


def test_reply_id_stored_and_resend_bounce_matches(assoc, board_person, fake_smtp):
    fake_smtp.reply = b"<abc123>"
    [d] = send(assoc, [board_person])
    assert d.status == Delivery.Status.SENT
    assert d.provider_id == "abc123"
    assert d.message_id.startswith("<")
    hit = bounces.handle_resend(assoc, _resend_bounce(email_id="abc123"))
    assert hit is not None and hit.pk == d.pk
    d.refresh_from_db()
    assert d.status == Delivery.Status.BOUNCED


def test_message_id_fallback_when_reply_has_id(assoc, board_person, fake_smtp):
    fake_smtp.reply = b"<abc123>"
    [d] = send(assoc, [board_person])
    hit = bounces.handle_resend(assoc, _resend_bounce(email_id="zzz", message_id=d.message_id))
    assert hit is not None and hit.pk == d.pk


def test_generic_reply_keeps_message_id_matching(assoc, board_person, fake_smtp):
    fake_smtp.reply = b"OK"
    [d] = send(assoc, [board_person])
    assert d.provider_id == d.message_id and d.provider_id.startswith("<")
    assert bounces.mark_bounced(assoc, d.message_id).pk == d.pk


def test_mark_bounced_ignores_empty_id(assoc):
    assert bounces.mark_bounced(assoc, "") is None
