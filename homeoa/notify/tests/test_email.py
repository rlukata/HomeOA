import datetime as dt
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.urls import reverse

from homeoa.access import permissions as perms
from homeoa.access.services import assign_role, ensure_default_roles
from homeoa.access.testing import close_residents, open_residents
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos as dashboard_todos
from homeoa.jobs.testing import capture_jobs
from homeoa.notify import bounces, commlog, types
from homeoa.notify import services as notify_services
from homeoa.notify.models import Delivery
from homeoa.plugins import services as plugin_services
from homeoa.registry import services as registry_services
from homeoa.registry.models import NoticePreference, Person, Unit

AS_OF = dt.date(2026, 10, 7)
SECRET = "s3cret-s3cret-s3cret"

NOTICE = types.NotificationType(
    key="test.email_notice",
    label="Notice",
    default_channels=("email",),
    mandatory=True,
    subject_template="Notice for unit {{ unit_number }}",
    body_template="Hello {{ recipient.full_name }} & co.",
)


@pytest.fixture(autouse=True)
def registered():
    types.register(NOTICE)
    yield
    types.unregister(NOTICE.key)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    ensure_default_roles(a)
    with association_context(a):
        yield a


def make_person(assoc, name, email):
    p = Person.objects.create(association=assoc, full_name=name, email=email)
    NoticePreference.objects.create(
        association=assoc,
        person=p,
        delivery="email",
        e_delivery_consent=True,
        email=email,
        source="portal",
    )
    return p


@pytest.fixture
def unit(assoc):
    return Unit.objects.create(number="C", allocated_interest=Decimal("10.00"))


@pytest.fixture
def board_user(assoc):
    user = get_user_model().objects.create_user(email="bd@example.com")
    assign_role(user, assoc, perms.BOARD, start=AS_OF - dt.timedelta(days=30))
    return user


@pytest.fixture
def board_person(assoc, board_user, unit):
    p = make_person(assoc, "Priya Board", "bd@example.com")
    p.user = board_user
    p.save()
    registry_services.set_initial_ownership(unit, [p], AS_OF - dt.timedelta(days=400))
    return p


@pytest.fixture
def owner(assoc, unit):
    return make_person(assoc, "Olive Owner", "olive@example.com")


def enable_residents(assoc):
    open_residents(assoc)


def send(assoc, people, unit=None):
    with capture_jobs() as queue:
        notes = notify_services.notify(
            NOTICE.key, people, {"unit_number": "C"}, AS_OF, unit=unit, period="2026-10"
        )
        assert [d.status for n in notes for d in n.deliveries.all()] == ["queued"] * len(notes)
        queue.run_all()
    return [d for n in notes for d in n.deliveries.all()]


def enable_bounces(assoc):
    plugin_services.save_installation(
        assoc, bounces.PLUGIN_ID, enabled=True, data={"secret": SECRET}
    )


# ------------------------------------------------------------------ sending


def test_send_success_html_and_text(assoc, board_person, unit):
    [d] = send(assoc, [board_person])
    assert d.status == Delivery.Status.SENT
    assert d.sent_at is not None and d.provider_id and d.address == "bd@example.com"
    assert d.notification.unit == unit  # owner of exactly one unit
    [msg] = mail.outbox
    assert msg.to == ["bd@example.com"]
    assert msg.subject == "Notice for unit C"
    assert "Hello Priya Board & co." in msg.body
    assert "Cedar Row" in msg.body
    html, mime = msg.alternatives[0]
    assert mime == "text/html" and "Hello Priya Board &amp; co." in html
    assert msg.extra_headers["Message-ID"] == d.provider_id


def test_send_failure_marks_failed_and_todo(assoc, board_person, monkeypatch):
    from homeoa.providers.defaults.smtp import SmtpEmail
    from homeoa.providers.email import EmailResult

    monkeypatch.setattr(SmtpEmail, "send", lambda self, m: EmailResult(sent=False, error="boom"))
    [d] = send(assoc, [board_person])
    assert d.status == Delivery.Status.FAILED and d.error == "boom"
    assert any(i["id"] == f"notify-failed-{d.pk}" for i in dashboard_todos_raw(assoc))


def dashboard_todos_raw(assoc):
    from homeoa.notify.todos import todo_items

    return todo_items(assoc)


def test_residents_off_never_emailed(assoc, owner, unit):
    notes = notify_services.notify(NOTICE.key, [owner], {}, AS_OF, unit=unit)
    [d] = notes[0].deliveries.all()
    assert d.status == Delivery.Status.PREVIEW
    assert mail.outbox == []


def test_job_rechecks_gate(assoc, owner, unit):
    enable_residents(assoc)
    with capture_jobs() as queue:
        notify_services.notify(NOTICE.key, [owner], {}, AS_OF, unit=unit)
        close_residents(assoc)
        queue.run_all()
    d = Delivery.objects.get()
    assert d.status == Delivery.Status.PREVIEW and mail.outbox == []


def test_residents_on_owner_emailed(assoc, owner, unit):
    enable_residents(assoc)
    [d] = send(assoc, [owner], unit=unit)
    assert d.status == Delivery.Status.SENT and mail.outbox[0].to == ["olive@example.com"]


# ------------------------------------------------------------------ bounces


def post_bounce(client, assoc, body, secret=SECRET):
    return client.post(
        f"/hooks/{bounces.PLUGIN_ID}/{assoc.pk}/",
        data=body,
        content_type="application/json",
        headers={bounces.SIGNATURE_HEADER: bounces.sign(body, secret)},
    )


def test_bounce_webhook_good_signature(client, assoc, board_person, unit):
    enable_bounces(assoc)
    [d] = send(assoc, [board_person])
    body = bounces.payload_for(d, event_id="evt-1", reason="550 no such user")
    assert post_bounce(client, assoc, body).status_code == 200
    d.refresh_from_db()
    assert d.status == Delivery.Status.BOUNCED and d.bounced_at and d.error == "550 no such user"
    # repeat is acknowledged, not reprocessed
    assert post_bounce(client, assoc, body).content == b"duplicate"


def test_bounce_webhook_bad_signature(client, assoc, board_person):
    enable_bounces(assoc)
    [d] = send(assoc, [board_person])
    body = bounces.payload_for(d, event_id="evt-2")
    assert post_bounce(client, assoc, body, secret="wrong-wrong-wrong-wrong").status_code == 400
    d.refresh_from_db()
    assert d.status == Delivery.Status.SENT


def test_bounce_webhook_needs_enabled_builtin(client, assoc, board_person):
    [d] = send(assoc, [board_person])
    assert post_bounce(client, assoc, bounces.payload_for(d)).status_code == 404


def test_bounce_todo_appears_and_resolves(client, assoc, board_user, board_person, unit):
    enable_bounces(assoc)
    [d] = send(assoc, [board_person])
    bounces.mark_bounced(assoc, d.provider_id, "550")
    items = [i for i in dashboard_todos.collect(assoc) if i.id == f"notify-bounced-{d.pk}"]
    assert [i.title for i in items] == ["Email to unit C bounced"]
    client.force_login(board_user)
    resp = client.post(reverse("notify:delivery_handled", args=[d.pk]))
    assert resp.status_code == 302
    d.refresh_from_db()
    assert d.resolved_at is not None
    assert not any(i.id == f"notify-bounced-{d.pk}" for i in dashboard_todos.collect(assoc))


def test_e2e_bounce_command(assoc, board_person, monkeypatch):
    monkeypatch.setenv("HOMEOA_E2E", "1")
    [d] = send(assoc, [board_person])
    out = StringIO()
    call_command("e2e_bounce", "BD@example.com", stdout=out)
    d.refresh_from_db()
    assert d.status == Delivery.Status.BOUNCED
    assert "bounced" in out.getvalue()


# ------------------------------------------------------------------ comm log


def test_commlog_lists_and_filters(assoc, board_person, owner, unit):
    send(assoc, [board_person])
    notify_services.notify(NOTICE.key, [owner], {}, AS_OF, unit=unit)  # preview
    rows = list(commlog.entries(unit))
    assert {r.status for r in rows} == {"sent", "preview"}
    assert [r.status for r in commlog.entries(unit, commlog.Filters(status="preview"))] == [
        "preview"
    ]
    assert list(commlog.entries(unit, commlog.Filters(channel="postal"))) == []


def test_commlog_view_board(client, assoc, board_user, board_person, unit):
    send(assoc, [board_person])
    client.force_login(board_user)
    resp = client.get(reverse("notify:unit_comms", args=["C"]))
    assert resp.status_code == 200
    assert b'data-testid="comm-log"' in resp.content
    assert b"Notice for unit C" in resp.content
    csv = client.get(reverse("notify:unit_comms", args=["C"]) + "?format=csv")
    assert csv["Content-Type"].startswith("text/csv")
    assert "Notice for unit C" in csv.content.decode()
    page = client.get(reverse("registry:unit", args=["C"]))
    assert reverse("notify:unit_comms", args=["C"]).encode() in page.content


def test_commlog_view_denied_to_owner(client, assoc, unit):
    user = get_user_model().objects.create_user(email="own@example.com")
    p = make_person(assoc, "Olive Owner", "own@example.com")
    p.user = user
    p.save()
    registry_services.set_initial_ownership(unit, [p], AS_OF - dt.timedelta(days=400))
    client.force_login(user)
    url = reverse("notify:unit_comms", args=["C"])
    # Residents off: the gate sends resident-only users to /me/.
    resp = client.get(url)
    assert resp.status_code == 302
    assert resp.url == reverse("portal:home")
    # Residents on: the view itself refuses an owner.
    open_residents(assoc)
    assert client.get(url).status_code == 403
    client.logout()
    assert client.get(reverse("notify:unit_comms", args=["C"])).status_code == 302


@pytest.mark.parametrize("residents_on", [False, True])
def test_preview_only_never_sends_even_to_board(assoc, board_person, unit, residents_on):
    if residents_on:
        enable_residents(assoc)
    with capture_jobs() as queue:
        notes = notify_services.notify(
            NOTICE.key, [board_person], {}, AS_OF, unit=unit, preview_only=True
        )
        assert queue.pending() == []
        queue.run_all()
    [d] = notes[0].deliveries.all()
    assert d.status == Delivery.Status.PREVIEW
    assert mail.outbox == []
