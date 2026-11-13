import datetime as dt
import hashlib
import io
import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client
from django.utils import timezone
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from homeoa import providers
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.esign import services
from homeoa.esign.models import Envelope, EventImmutableError, SignatureEvent, Signer
from homeoa.providers import get_provider
from homeoa.providers.esign import SignatureRequest, SignatureStatus
from homeoa.providers.esign import Signer as ReqSigner

START = dt.date(2020, 1, 1)


def _pdf(text="Minutes of the board meeting") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def assoc(transactional_db, settings, tmp_path):
    # transactional_db: invite emails go out in on_commit, so tests need real commits.
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def source(assoc):
    return get_provider("FileStorage", assoc).put(
        assoc.pk, "minutes.pdf", _pdf(), content_type="application/pdf", folder="test"
    )


def _env(assoc, source, routing=Envelope.Routing.SEQUENTIAL, n=2, **kw):
    signers = [{"name": f"Signer {i}", "email": f"s{i}@example.org"} for i in range(1, n + 1)]
    return services.create_envelope(
        assoc,
        title="March minutes",
        source_key=source.key,
        source_name="minutes.pdf",
        signers=signers,
        routing=routing,
        **kw,
    )


def _tokens():
    return [re.search(r"/sign/([^/\s]+)/", m.body).group(1) for m in mail.outbox]


def _sign(token, name="Signer"):
    return services.sign(
        token, typed_name=name, consent=True, ip="203.0.113.5", user_agent="TestBrowser/1.0"
    )


def test_sequential_flow_produces_certificate(assoc, source):
    env = _env(assoc, source)
    services.send(env)
    stored = get_provider("FileStorage", assoc).get(source.key)
    assert env.source_sha256 == source.sha256 == hashlib.sha256(stored).hexdigest()
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["s1@example.org"]
    [t1] = _tokens()
    _sign(t1, "Signer One")
    assert len(mail.outbox) == 2 and mail.outbox[1].to == ["s2@example.org"]
    t2 = _tokens()[1]
    _sign(t2, "Signer Two")
    env.refresh_from_db()
    assert env.status == Envelope.Status.COMPLETED
    out = get_provider("FileStorage", assoc).get(env.result_key)
    assert hashlib.sha256(out).hexdigest() == env.result_sha256
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 2
    cert = reader.pages[1].extract_text()
    for needle in (
        "Signature certificate",
        "Signer One",
        "s2@example.org",
        "203.0.113.5",
        "TestBrowser/1.0",
        env.source_sha256,
        "UTC",
    ):
        assert needle in cert
    kinds = list(env.events.values_list("kind", flat=True))
    assert kinds[0] == "created" and kinds[-1] == "completed"
    assert kinds.count("signed") == 2
    done = env.events.get(kind="completed")
    assert done.data["result_sha256"] == env.result_sha256
    assert AuditEvent.objects.filter(action="esign.completed").exists()


def test_token_single_use(assoc, source):
    env = _env(assoc, source, n=1)
    services.send(env)
    [t] = _tokens()
    _sign(t)
    with pytest.raises(services.LinkInvalid):
        _sign(t)
    with pytest.raises(services.LinkInvalid):
        services.signer_for_token("bogus")


def test_token_expiry(assoc, source, settings):
    env = _env(assoc, source, n=1)
    services.send(env)
    [t] = _tokens()
    Signer.objects.filter(envelope=env).update(
        token_expires_at=timezone.now() - dt.timedelta(seconds=1)
    )
    with pytest.raises(services.LinkInvalid, match="expired"):
        _sign(t)


def test_order_enforced(assoc, source):
    env = _env(assoc, source)
    services.send(env)
    s2 = env.signers.get(order=2)
    assert s2.status == Signer.Status.WAITING and not s2.token_hash
    # Even with a token issued out of turn, the second signer cannot sign first.
    t2 = services.issue_token(s2)
    with pytest.raises(services.ESignError, match="earlier signer"):
        _sign(t2)


def test_parallel_invites_everyone(assoc, source):
    env = _env(assoc, source, routing=Envelope.Routing.PARALLEL, n=3)
    services.send(env)
    tokens = _tokens()
    assert len(tokens) == 3
    _sign(tokens[2])
    _sign(tokens[0])
    _sign(tokens[1])
    env.refresh_from_db()
    assert env.status == Envelope.Status.COMPLETED


def test_decline(assoc, source):
    env = _env(assoc, source)
    services.send(env)
    [t] = _tokens()
    services.decline(t, reason="Wrong date", ip="203.0.113.5")
    env.refresh_from_db()
    assert env.status == Envelope.Status.DECLINED
    assert env.events.filter(kind="declined", data__reason="Wrong date").exists()
    with pytest.raises(services.LinkInvalid):
        _sign(t)


def test_void_kills_links(assoc, source):
    env = _env(assoc, source, routing=Envelope.Routing.PARALLEL)
    services.send(env)
    tokens = _tokens()
    services.void(env, reason="Superseded")
    for t in tokens:
        with pytest.raises(services.LinkInvalid):
            _sign(t)
    with pytest.raises(services.ESignError):
        services.void(env)


def test_tampered_original_detected(assoc, source, settings):
    env = _env(assoc, source, n=1)
    services.send(env)
    [t] = _tokens()
    from pathlib import Path

    Path(settings.HOMEOA_FILE_ROOT, source.key).write_bytes(_pdf("Altered minutes"))
    with pytest.raises(services.TamperedError):
        _sign(t)
    signer = env.signers.get()
    assert signer.status == Signer.Status.INVITED
    assert env.events.filter(kind="tamper").exists()


def test_consent_and_name_required(assoc, source):
    env = _env(assoc, source, n=1)
    services.send(env)
    [t] = _tokens()
    with pytest.raises(services.ESignError):
        services.sign(t, typed_name="X", consent=False)
    with pytest.raises(services.ESignError):
        services.sign(t, typed_name="  ", consent=True)
    _sign(t)  # still usable


def test_events_append_only(assoc, source):
    env = _env(assoc, source)
    ev = env.events.first()
    with pytest.raises(EventImmutableError):
        ev.save()
    with pytest.raises(EventImmutableError):
        ev.delete()
    with pytest.raises(EventImmutableError):
        SignatureEvent.objects.filter(pk=ev.pk).update(kind="x")


calls = []


def _callback(envelope):
    calls.append((envelope.pk, envelope.on_complete_ref, envelope.result_key))


def test_on_complete_callback(assoc, source, django_capture_on_commit_callbacks):
    calls.clear()
    env = _env(assoc, source, n=1, on_complete=f"{__name__}._callback", on_complete_ref="m-7")
    services.send(env)
    [t] = _tokens()
    with django_capture_on_commit_callbacks(execute=True):
        _sign(t)
    env.refresh_from_db()
    assert calls == [(env.pk, "m-7", env.result_key)]


def test_source_must_belong_to_association(assoc, source):
    other = Association.objects.create(name="Other", short_name="OT")
    with pytest.raises(services.ESignError):
        _env(other, source)


def test_provider_default(assoc, source):
    from homeoa.providers import get_provider as gp

    result = gp("SignatureProvider", assoc).request_signature(
        SignatureRequest(
            association_id=assoc.pk,
            title="Contract",
            document_key=source.key,
            signers=(ReqSigner(name="Pat", email="pat@example.org"),),
        )
    )
    assert result.status == SignatureStatus.REQUESTED
    env = Envelope.objects.get(pk=int(result.reference))
    assert env.status == Envelope.Status.SENT


# --- views ---------------------------------------------------------------------------------


@pytest.fixture
def board(assoc):
    user = get_user_model().objects.create_user(email="sec@example.org")
    grant(user, association=assoc, start=START, title="secretary")
    return user


def test_board_create_send_and_signer_page(client, assoc, board):
    client.force_login(board)
    resp = client.post(
        "/governance/signatures/new/",
        {
            "title": "March minutes",
            "routing": "sequential",
            "name_0": "Sam Secretary",
            "email_0": "sec@example.org",
            "file": SimpleUploadedFile("minutes.pdf", _pdf(), content_type="application/pdf"),
        },
    )
    env = Envelope.objects.get()
    assert resp.status_code == 302
    assert env.signers.get().user == board  # a user signer must log in
    client.post(f"/governance/signatures/{env.pk}/send/")
    env.refresh_from_db()
    assert env.status == Envelope.Status.SENT
    [t] = _tokens()
    assert client.get(f"/governance/signatures/{env.pk}/").status_code == 200

    client.logout()
    resp = client.get(f"/sign/{t}/")
    assert resp.status_code == 302 and "/login/" in resp["Location"]

    client.force_login(board)
    page = client.get(f"/sign/{t}/")
    assert page.status_code == 200
    assert b"Uniform Electronic Transactions Act" in page.content
    assert client.get(f"/sign/{t}/document/").content.startswith(b"%PDF")
    resp = client.post(
        f"/sign/{t}/", {"action": "sign", "typed_name": "Sam Secretary", "consent": "on"}
    )
    assert b'data-testid="signed"' in resp.content
    env.refresh_from_db()
    assert env.status == Envelope.Status.COMPLETED
    assert client.get(f"/sign/{t}/").status_code == 410


def test_anonymous_external_signer(client, assoc, source):
    env = _env(assoc, source, n=1)
    services.send(env)
    [t] = _tokens()
    resp = client.post(f"/sign/{t}/", {"action": "sign", "typed_name": "Ext"})
    assert b"sign-error" in resp.content  # no consent
    resp = client.post(f"/sign/{t}/", {"action": "sign", "typed_name": "Ext", "consent": "on"})
    assert resp.status_code == 200
    s = env.signers.get()
    assert s.status == Signer.Status.SIGNED and s.ip == "127.0.0.1"


def test_rolled_back_send_emails_nobody(assoc, source):
    env = _env(assoc, source, n=1)
    with pytest.raises(RuntimeError), transaction.atomic():
        services.send(env)
        raise RuntimeError("boom")
    assert mail.outbox == []
    env = Envelope.objects.get(pk=env.pk)
    assert env.status == Envelope.Status.DRAFT
    assert not env.events.filter(kind=SignatureEvent.Kind.INVITED).exists()


def test_failed_invite_email_does_not_raise(client, assoc, source, board, monkeypatch):
    class Boom:
        def send(self, request):
            raise OSError("smtp down")

    real = providers.get_provider
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda name, a: Boom() if name == "EmailChannel" else real(name, a),
    )
    env = _env(assoc, source, n=1)
    services.send(env)
    assert env.signers.get().status == Signer.Status.INVITED
    assert env.events.filter(kind=SignatureEvent.Kind.INVITED).count() == 1
    failed = env.events.get(kind=SignatureEvent.Kind.INVITE_FAILED)
    assert failed.data["error"] == "smtp down"
    assert AuditEvent.objects.filter(action="esign.invite_failed").exists()
    client.force_login(board)
    page = client.get(f"/governance/signatures/{env.pk}/")
    assert b'data-testid="invite-failed"' in page.content


def test_sign_post_passes_csrf_with_origin(assoc, source):
    env = _env(assoc, source, n=1)
    services.send(env)
    [t] = _tokens()
    c = Client(enforce_csrf_checks=True)
    page = c.get(f"/sign/{t}/")
    assert page["Referrer-Policy"] == "same-origin"
    assert page["Cache-Control"] == "no-store"
    csrf = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1)
    resp = c.post(
        f"/sign/{t}/",
        {
            "csrfmiddlewaretoken": csrf.decode(),
            "action": "sign",
            "typed_name": "Ext",
            "consent": "on",
        },
        HTTP_ORIGIN="http://testserver",
    )
    assert resp.status_code == 200
    assert env.signers.get().status == Signer.Status.SIGNED


def test_board_pages_need_permission(client, assoc):
    user = get_user_model().objects.create_user(email="nobody@example.org")
    client.force_login(user)
    assert client.get("/governance/signatures/").status_code == 403


# --- DB-level append-only and the review to-do -------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE esign_signatureevent SET kind = 'x' WHERE id = %s",
        "DELETE FROM esign_signatureevent WHERE id = %s",
    ],
)
def test_events_append_only_in_database(assoc, source, sql):
    from django.db import DatabaseError, connection

    env = _env(assoc, source)
    ev = env.events.first()
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cur,
    ):
        cur.execute(sql, [ev.pk])
    assert SignatureEvent.objects.get(pk=ev.pk).kind == "created"


def _todos(assoc):
    from homeoa.dashboard import todos

    return {i.id: i for i in todos.collect(assoc) if i.source == "esign"}


def test_completed_envelope_todo_until_reviewed(assoc, source):
    env = _env(assoc, source, n=1)
    services.send(env)
    assert _todos(assoc) == {}
    _sign(_tokens()[0], "Signer One")
    item = _todos(assoc)[f"esign-review-{env.pk}"]
    assert "March minutes" in item.title and item.kind == "gov"
    assert item.post_url == f"/governance/signatures/{env.pk}/reviewed/"
    env.refresh_from_db()
    services.mark_reviewed(env)
    services.mark_reviewed(env)  # idempotent
    assert _todos(assoc) == {}
    assert env.events.filter(kind="reviewed").count() == 1
    assert AuditEvent.objects.filter(action="esign.reviewed").count() == 1


def test_declined_envelope_todo_names_signer(assoc, source):
    env = _env(assoc, source)
    services.send(env)
    services.decline(_tokens()[0], reason="Wrong date")
    item = _todos(assoc)[f"esign-review-{env.pk}"]
    assert "declined" in item.title.lower()
    assert "Signer 1" in item.sub and "Wrong date" in item.sub


def test_open_or_voided_envelope_has_no_review(assoc, source):
    env = _env(assoc, source)
    with pytest.raises(services.ESignError):
        services.mark_reviewed(env)
    services.void(env)
    assert _todos(assoc) == {}


def test_review_from_home_todo(client, assoc, source, board):
    env = _env(assoc, source, n=1)
    services.send(env)
    services.decline(_tokens()[0])
    client.force_login(board)
    page = client.get(f"/governance/signatures/{env.pk}/")
    assert b'data-testid="esign-reviewed"' in page.content
    resp = client.post(f"/todos/esign-review-{env.pk}/act/")
    assert resp.status_code in (200, 302)
    assert env.events.filter(kind="reviewed").exists()
    page = client.get(f"/governance/signatures/{env.pk}/")
    assert b'data-testid="esign-reviewed"' not in page.content


def test_review_needs_manage_permission(client, assoc, source):
    env = _env(assoc, source, n=1)
    services.send(env)
    services.decline(_tokens()[0])
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    resp = client.post(f"/governance/signatures/{env.pk}/reviewed/")
    assert resp.status_code in (302, 403)
    assert not env.events.filter(kind="reviewed").exists()


def test_demo_reset_with_signature_events(transactional_db, settings, tmp_path):
    """seed_demo --reset TRUNCATEs (row triggers don't fire); plain DELETE still fails."""
    from django.core.management import call_command
    from django.db import DatabaseError, connection

    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    call_command("seed_demo", "--reset")
    a = Association.objects.get()
    with association_context(a):
        src = get_provider("FileStorage", a).put(
            a.pk, "minutes.pdf", _pdf(), content_type="application/pdf", folder="test"
        )
        services.send(_env(a, src))
        _sign(_tokens()[0], name="Signer 1")
    assert SignatureEvent.objects.filter(kind="signed").exists()

    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cur,
    ):
        cur.execute("DELETE FROM esign_signatureevent")

    call_command("seed_demo", "--reset")
    assert SignatureEvent.objects.count() == 0
    assert Association.objects.count() == 1
