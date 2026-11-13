from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction

from homeoa import audit
from homeoa.audit.models import AuditEvent, AuditImmutableError
from homeoa.audit.services import unregister
from homeoa.core.context import association_context
from homeoa.core.models import Association
from tests.scoped.models import Secret, Widget


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Olympia Condo", short_name="TC")
    with association_context(a):
        yield a


@pytest.fixture
def audited(assoc):
    audit.register(Widget)
    audit.register(Secret, exclude=["hidden"])
    yield
    unregister(Widget)
    unregister(Secret)


def _user(email):
    model = get_user_model()
    fields = {model.USERNAME_FIELD: email}
    if model.USERNAME_FIELD != "email" and hasattr(model, "email"):
        fields["email"] = email
    return model.objects.create_user(**fields, password="pw-test")  # noqa: S106


def _events(**kw):
    return list(AuditEvent.objects.filter(**kw).order_by("pk"))


def test_create_update_delete_write_one_event_each(audited, assoc):
    w = Widget.objects.create(name="Roof", amount=Decimal("10.00"))
    (created,) = _events(action="create", object_type="scoped.widget")
    assert created.object_type == "scoped.widget"
    assert created.object_id == str(w.pk)
    assert created.association == assoc
    assert created.changes["name"] == [None, "Roof"]
    assert created.changes["amount"] == [None, "10.00"]

    w.amount = Decimal("12.50")
    w.save()
    (updated,) = _events(action="update", object_type="scoped.widget")
    assert updated.changes == {"amount": ["10.00", "12.50"]}

    w.save()  # no change, no event
    assert len(_events(action="update", object_type="scoped.widget")) == 1

    pk = w.pk
    w.delete()
    (deleted,) = _events(action="delete", object_type="scoped.widget")
    assert deleted.object_id == str(pk)
    assert deleted.changes["name"] == ["Roof", None]


def test_encrypted_fields_never_in_diff(audited):
    s = Secret.objects.create(label="a", note="ssn-111", data={"k": "v"}, hidden="h1")
    s.note = "ssn-222"
    s.hidden = "h2"
    s.save()
    created, updated = _events(object_type="scoped.secret")
    assert created.changes["note"] == "changed"
    assert created.changes["data"] == "changed"
    assert "hidden" not in created.changes
    assert updated.changes == {"note": "changed"}
    with connection.cursor() as cur:
        cur.execute("SELECT changes::text FROM audit_auditevent")
        dump = " ".join(r[0] for r in cur.fetchall())
    assert "ssn-" not in dump
    # Changing only an excluded field writes nothing.
    s.hidden = "h3"
    s.save()
    assert len(_events(object_type="scoped.secret")) == 2


def test_association_settings_are_audited(assoc):
    assoc.settings = {"late_fee": "25.00"}
    assoc.save()
    (event,) = _events(action="update", object_type="core.association")
    assert event.changes["settings"] == [{}, {"late_fee": "25.00"}]
    assert event.association == assoc


def test_record_explicit_event_with_actor(assoc):
    user = _user("treasurer@example.org")
    with audit.audit_context(actor=user, ip="10.0.0.5", request_id="req-1"):
        ev = audit.record("payment.void", assoc, message="Duplicate", changes={"amt": Decimal(5)})
    assert ev.pk
    assert ev.actor_id == user.pk
    assert ev.actor_label == str(user)
    assert ev.ip == "10.0.0.5"
    assert ev.request_id == "req-1"
    assert ev.changes == {"amt": "5"}
    bare = audit.record("match.confirm", object_type="banking.match", object_id=7)
    assert bare.association == assoc
    assert bare.actor_id is None


def test_model_level_immutability(assoc):
    ev = audit.record("setting.change")
    ev.action = "x"
    with pytest.raises(AuditImmutableError):
        ev.save()
    with pytest.raises(AuditImmutableError):
        ev.delete()
    with pytest.raises(AuditImmutableError):
        AuditEvent.objects.all().update(action="x")
    with pytest.raises(AuditImmutableError):
        AuditEvent.objects.all().delete()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_auditevent SET action = 'tampered'",
        "DELETE FROM audit_auditevent",
    ],
)
def test_trigger_blocks_raw_sql(assoc, sql):
    audit.record("setting.change")
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cur,
    ):
        cur.execute(sql)
    assert AuditEvent.objects.filter(action="setting.change").count() == 1


def test_middleware_sets_request_context(assoc, client, settings):
    from django.contrib.auth import get_user_model

    client.force_login(get_user_model().objects.create_user("board@example.org"))
    resp = client.get("/settings/audit/", HTTP_X_REQUEST_ID="abc-123")
    assert resp["X-Request-ID"] == "abc-123"
    resp = client.get("/settings/audit/", HTTP_X_REQUEST_ID="bad id; drop")
    assert resp["X-Request-ID"] != "bad id; drop"


def test_log_view_table_and_filters(audited, assoc, client, settings):
    from django.contrib.auth import get_user_model

    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("board@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    Widget.objects.create(name="Roof")
    audit.record("payment.record", object_type="payments.payment", object_id=1)
    assoc.settings = {"x": 1}
    assoc.save()
    resp = client.get("/settings/audit/")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "<table" in html and "<th" in html
    assert "payment.record" in html
    assert "Association settings" in html
    assert "edit" not in html.lower().replace("edited", "")
    resp = client.get("/settings/audit/", {"action": "payment"})
    page = resp.context["page_obj"]
    assert [e.action for e in page] == ["payment.record"]
    resp = client.get("/settings/audit/", {"object_type": "scoped.widget"})
    assert [e.action for e in resp.context["page_obj"]] == ["create"]


def test_log_view_scoped_to_association(assoc, client, settings):
    from django.contrib.auth import get_user_model

    client.force_login(get_user_model().objects.create_user("board@example.org"))
    other = Association.objects.create(name="Other", short_name="O")
    audit.record("payment.other", association=other)
    # Middleware resolves the first association (assoc).
    resp = client.get("/settings/audit/")
    assert "payment.other" not in resp.content.decode()


def test_log_view_gated(db, client, settings):
    assert client.get("/settings/audit/").status_code == 302  # to /login/


def _request_through_middleware(view, rf):
    from django.contrib.auth.middleware import AuthenticationMiddleware
    from django.contrib.sessions.middleware import SessionMiddleware
    from django.core.handlers.exception import convert_exception_to_response

    from homeoa.audit.middleware import AuditContextMiddleware

    handler = SessionMiddleware(
        AuthenticationMiddleware(AuditContextMiddleware(convert_exception_to_response(view)))
    )
    return handler(rf.get("/x/", HTTP_X_REQUEST_ID="rid-42"))


def test_login_mid_request_records_new_user(assoc, rf):
    from django.contrib.auth import login, logout
    from django.http import HttpResponse

    user = _user("owner@example.org")
    seen = {}

    def view(request):
        assert not request.user.is_authenticated
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        seen["in"] = audit.record("auth.login")
        logout(request)
        seen["out"] = audit.record("auth.logout_done")
        return HttpResponse("ok")

    resp = _request_through_middleware(view, rf)
    assert resp.status_code == 200
    assert seen["in"].actor_id == user.pk
    assert seen["in"].request_id == "rid-42"
    assert seen["out"].actor_id is None


def test_request_id_on_error_response_and_logs(assoc, rf, caplog):
    import logging

    from homeoa.audit.context import RequestIdLogFilter

    def view(request):
        raise RuntimeError("boom")

    logger = logging.getLogger("django.request")
    filt = RequestIdLogFilter()
    logger.addFilter(filt)
    try:
        with caplog.at_level(logging.ERROR, logger="django.request"):
            resp = _request_through_middleware(view, rf)
    finally:
        logger.removeFilter(filt)
    assert resp.status_code == 500
    assert resp["X-Request-ID"] == "rid-42"
    assert any(getattr(r, "request_id", None) == "rid-42" for r in caplog.records)


def test_system_label_without_user(assoc):
    with audit.audit_context(label="job:send_statements"):
        ev = audit.record("statement.send")
    assert ev.actor_id is None
    assert ev.actor_label == "job:send_statements"
