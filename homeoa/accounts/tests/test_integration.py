"""Accounts wired to audit, crypto, events and jobs."""

import datetime as dt
import re
import time
from types import SimpleNamespace

import pyotp
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection

from homeoa import audit
from homeoa.accounts import events as user_events
from homeoa.accounts import services, signals
from homeoa.accounts.management.e2e_guard import SANDBOX_MODE_KEY
from homeoa.accounts.models import TOTPDevice
from homeoa.accounts.tasks import SEND_MAGIC_LINK
from homeoa.audit.models import AuditEvent
from homeoa.core.crypto import decrypt
from homeoa.core.models import Association
from homeoa.events import publish, subscribe, unsubscribe
from homeoa.jobs import periodic, run_due
from homeoa.jobs.scheduler import unregister
from homeoa.jobs.testing import capture_jobs

pytestmark = pytest.mark.django_db


@pytest.fixture
def assoc():
    return Association.objects.create(name="Cedar Row", short_name="CR")


@pytest.fixture
def user():
    return get_user_model().objects.create_user("board@example.org")


def events(action):
    return list(AuditEvent.all_associations.filter(action=action).order_by("pk"))


def request_link(client, email, capture):
    with capture_jobs() as queue, capture(execute=True):
        resp = client.post("/login/", {"email": email})
    queue.run_all()
    return resp


def link_from_mail():
    return re.search(r"https?://\S+", mail.outbox[-1].body).group(0)


def enrol(user):
    device = services.start_enrolment(user)
    codes = services.confirm_enrolment(device, pyotp.TOTP(device.secret).now())
    device.refresh_from_db()
    return device, codes


def next_code(device):
    # enrol() used the current step; the next one is within the allowed drift.
    return pyotp.TOTP(device.secret).at(time.time() + 30)


# --- (a) encrypted TOTP secret ------------------------------------------------------


def raw_secret(device):
    with connection.cursor() as cur:
        cur.execute("SELECT secret FROM accounts_totpdevice WHERE id = %s", [device.pk])
        return cur.fetchone()[0]


def test_totp_secret_encrypted_at_rest(user):
    device, _codes = enrol(user)
    stored = raw_secret(device)
    assert device.secret not in stored
    assert decrypt(stored) == device.secret
    assert TOTPDevice.objects.get().secret == device.secret


def test_totp_audit_diff_hides_secret(user):
    device = services.start_enrolment(user)
    created = AuditEvent.all_associations.get(object_type="accounts.totpdevice")
    assert created.changes["secret"] == "changed"
    assert device.secret not in str(created.changes)


def test_data_migration_encrypts_plaintext_rows(user):
    import importlib

    migration = importlib.import_module("homeoa.accounts.migrations.0002_encrypt_totp_secret")
    device = services.start_enrolment(user)
    plain = device.secret
    with connection.cursor() as cur:
        cur.execute("UPDATE accounts_totpdevice SET secret = %s WHERE id = %s", [plain, device.pk])
    editor = SimpleNamespace(connection=connection)
    migration.encrypt_existing(None, editor)
    once = raw_secret(device)
    assert once != plain and decrypt(once) == plain
    migration.encrypt_existing(None, editor)  # idempotent: tokens are left alone
    assert raw_secret(device) == once
    migration.decrypt_existing(None, editor)
    assert raw_secret(device) == plain


@pytest.fixture
def published():
    seen = []

    def handler(event):
        seen.append(event)

    subscribe("*", handler)
    yield seen
    unsubscribe("*", handler)


# --- (b) audit receivers ------------------------------------------------------------


def test_link_request_audited_without_association_or_hint(
    client, assoc, user, django_capture_on_commit_callbacks
):
    request_link(client, "board@example.org", django_capture_on_commit_callbacks)
    request_link(client, "nobody@example.org", django_capture_on_commit_callbacks)
    known, unknown = events("auth.link_requested")
    assert known.association_id is None and unknown.association_id is None
    assert known.object_id == str(user.pk)
    assert unknown.object_id == "" and unknown.actor_id is None
    assert "nobody" not in str(unknown.changes) + unknown.object_repr + unknown.message
    assert known.changes == unknown.changes == {"throttled": False}


def test_rejected_link_audited_with_null_association(client, assoc):
    client.post("/login/verify/garbage/")
    (event,) = events("auth.link_rejected")
    assert event.association_id is None and event.changes == {"reason": "invalid"}


def test_mfa_events_audited(client, assoc, user, published):
    client.force_login(user)
    html = client.get("/account/mfa/").content.decode()
    secret = re.search(r'data-testid="totp-secret">([A-Z2-7]+)<', html).group(1)
    client.post("/account/mfa/", {"action": "confirm", "code": pyotp.TOTP(secret).now()})
    client.post("/account/mfa/", {"action": "regenerate"})
    client.post("/account/mfa/", {"action": "disable"})
    for action in ("auth.mfa_enrolled", "auth.recovery_codes_regenerated", "auth.mfa_disabled"):
        (event,) = events(action)
        assert event.actor_id == user.pk and event.association_id == assoc.pk
    mfa = [e.name for e in published if e.name.startswith("user.mfa")]
    assert mfa == ["user.mfa_enrolled", "user.mfa_disabled"]


def test_mfa_challenge_failures_and_lockout(client, assoc, user):
    enrol(user)
    seen = []
    handler = lambda **kw: seen.append(kw["user"])  # noqa: E731
    signals.mfa_lockout.connect(handler)
    try:
        client.post(f"/login/verify/{services.create_magic_link(user)}/")
        for _i in range(5):
            client.post("/login/mfa/", {"code": "000000"})
    finally:
        signals.mfa_lockout.disconnect(handler)
    assert seen == [user]
    assert len(events("auth.mfa_failed")) == 5
    (lock,) = events("auth.mfa_lockout")
    assert lock.association_id is None and lock.object_id == str(user.pk)


def test_mfa_pass_audited(client, assoc, user):
    device, _codes = enrol(user)
    client.post(f"/login/verify/{services.create_magic_link(user)}/")
    client.post("/login/mfa/", {"code": next_code(device)})
    (event,) = events("auth.mfa_passed")
    assert event.changes == {"method": "totp"}


# --- (c) domain events --------------------------------------------------------------


def test_user_events_published(client, assoc, user, published):
    device, _codes = enrol(user)
    client.post(f"/login/verify/{services.create_magic_link(user)}/")
    client.post("/login/mfa/", {"code": "000000"})
    client.post("/login/mfa/", {"code": next_code(device)})
    client.post("/logout/")
    client.post("/login/verify/garbage/")
    names = [(e.name, getattr(e, "reason", None)) for e in published]
    assert names == [
        ("user.login_failed", "mfa"),
        ("user.logged_in", None),
        ("user.logged_out", None),
        ("user.login_failed", "invalid"),
    ]
    assert published[1].user_id == user.pk and published[1].association_id == assoc.pk
    assert published[-1].user_id is None


def test_user_events_registered():
    from homeoa.events import registry

    names = {"user.logged_in", "user.logged_out", "user.mfa_enrolled", "user.mfa_disabled"}
    assert names | {"user.login_failed"} <= set(registry())
    assert user_events.UserLoginFailed.name == "user.login_failed"


# --- (d) attribution end to end -----------------------------------------------------


def test_login_and_logout_attributed_to_user(
    client, assoc, user, django_capture_on_commit_callbacks
):
    request_link(client, "board@example.org", django_capture_on_commit_callbacks)
    resp = client.post(link_from_mail())
    assert resp.status_code == 302
    (login,) = events("auth.login")
    assert login.actor_id == user.pk and login.association_id == assoc.pk
    client.post("/logout/")
    (logout,) = events("auth.logout")
    assert logout.actor_id == user.pk and logout.request_id


def test_middleware_order():
    mw = settings.MIDDLEWARE
    auth = mw.index("django.contrib.auth.middleware.AuthenticationMiddleware")
    current = mw.index("homeoa.core.middleware.CurrentAssociationMiddleware")
    audit_mw = mw.index("homeoa.audit.middleware.AuditContextMiddleware")
    assert auth < current < audit_mw


# --- (e) jobs run in an audit context -----------------------------------------------


def test_periodic_job_labelled(assoc):
    def job(association, as_of):
        audit.record("test.job_ran", association=association)

    periodic("t.audited", "0 6 * * *")(job)
    try:
        run_due(as_of=dt.date(2026, 9, 11))
    finally:
        unregister("t.audited")
    (event,) = events("test.job_ran")
    assert event.actor_id is None and event.actor_label == "job:t.audited"


def _job_handler(event):
    audit.record("test.handler_ran", association=Association.objects.get(pk=event.association_id))


def test_job_mode_handler_labelled(assoc, user):
    subscribe("user.mfa_enrolled", _job_handler, mode="job")
    try:
        with capture_jobs() as queue:
            publish(user_events.UserMFAEnrolled(association_id=assoc.pk, user_id=user.pk))
            queue.run_all()
    finally:
        unsubscribe("user.mfa_enrolled", _job_handler)
    (event,) = events("test.handler_ran")
    path = "homeoa.accounts.tests.test_integration._job_handler"
    assert event.actor_label == f"job:{path}"


def test_tick_labelled(assoc):
    from homeoa.jobs.tasks import tick

    def job(association, as_of):
        audit.record("test.tick_ran", association=association)

    periodic("t.ticked", "* * * * *")(job)
    try:
        tick(timestamp=0)
    finally:
        unregister("t.ticked")
    assert {e.actor_label for e in events("test.tick_ran")} == {"job:t.ticked"}


# --- (f) magic-link email as an on_commit job ---------------------------------------


def test_link_email_sent_by_job_after_commit(
    client, assoc, user, django_capture_on_commit_callbacks
):
    with capture_jobs() as queue:
        with django_capture_on_commit_callbacks() as callbacks:
            client.post("/login/", {"email": "board@example.org"})
        assert queue.pending_names() == [] and callbacks
        for cb in callbacks:
            cb()
        assert queue.pending_names() == [SEND_MAGIC_LINK]
        assert "token" not in str(queue.pending()[0]["args"])
        assert mail.outbox == []
        queue.run_all()
    assert len(mail.outbox) == 1 and "/login/verify/" in mail.outbox[0].body


def test_unknown_address_queues_same_job_sends_nothing(
    client, assoc, django_capture_on_commit_callbacks
):
    with capture_jobs() as queue, django_capture_on_commit_callbacks(execute=True):
        client.post("/login/", {"email": "nobody@example.org"})
    assert queue.pending_names() == [SEND_MAGIC_LINK]
    queue.run_all()
    assert mail.outbox == []


def test_inline_setting_sends_at_commit(
    client, assoc, user, settings, django_capture_on_commit_callbacks
):
    settings.HOMEOA_JOBS_INLINE = True
    with capture_jobs() as queue, django_capture_on_commit_callbacks(execute=True):
        client.post("/login/", {"email": "board@example.org"})
    assert queue.pending_names() == []
    assert len(mail.outbox) == 1


def test_throttled_request_queues_nothing(
    client, assoc, user, monkeypatch, django_capture_on_commit_callbacks
):
    monkeypatch.setattr(services, "RATE_PER_EMAIL", 0)
    with capture_jobs() as queue, django_capture_on_commit_callbacks(execute=True):
        assert client.post("/login/", {"email": "board@example.org"}).status_code == 429
    assert queue.pending_names() == []


# --- (g) sandbox key ----------------------------------------------------------------


def test_sandbox_key():
    assert SANDBOX_MODE_KEY == "sandbox_mode"
