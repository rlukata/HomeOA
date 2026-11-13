"""Sending owner statements (FIN-04, COM-02, COM-05)."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos as dashboard_todos
from homeoa.jobs.testing import capture_jobs
from homeoa.letters.models import MailBatch
from homeoa.notify import bounces
from homeoa.notify.models import Delivery, Notification
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Unit
from homeoa.statements import owner_statements as svc
from homeoa.statements.models import Statement

User = get_user_model()
OCT = dt.date(2026, 10, 1)
TREASURER = "treasurer@cedarrow.example"


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        if not Charge.objects.filter(period=OCT).exists():
            generate_assessments(AssessmentSchedule.objects.get(), OCT, source="test")
        yield a


def _unit(n):
    return Unit.objects.get(number=n)


def _consent(unit_number):
    owner = reg.current_owners(_unit(unit_number))[0]
    owner.email = f"owner{unit_number.lower()}@example.com"
    owner.save()
    reg.record_notice_preference(
        owner, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )
    return owner


def _revoke(person):
    reg.record_notice_preference(
        person, NoticePreference.Delivery.POSTAL, source=NoticePreference.Source.PORTAL
    )


def _on(a):
    open_residents(a)


def _email_deliveries(statement):
    return Delivery.objects.filter(
        notification__type_key=svc.NOTIFICATION_TYPE,
        notification__object_key=svc.nsvc.object_key_for(statement),
    )


def test_send_refused_while_residents_off(pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    with pytest.raises(svc.ResidentsOff):
        svc.send_run(run)
    assert not run.statements.filter(status=Statement.Status.SENT).exists()
    assert set(Delivery.objects.values_list("status", flat=True)) == {Delivery.Status.PREVIEW}


def test_send_run_routes_by_consent(pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    _on(pilot)
    with capture_jobs() as queue:
        sent = svc.send_run(run)
        queue.run_all()
    assert len(sent) == run.statements.count()
    c = run.statements.get(unit__number="C")
    assert c.status == Statement.Status.SENT and c.sent_at is not None
    assert c.channel == Statement.Channel.EMAIL
    assert [d.status for d in _email_deliveries(c)] == [Delivery.Status.SENT]
    b = run.statements.get(unit__number="B")
    assert b.channel == Statement.Channel.MAIL
    assert b.mail_batch.status != MailBatch.Status.DRAFT
    assert b.mail_batch.items.count() == run.statements.filter(channel="mail").count()
    assert AuditEvent.objects.filter(action="statement.send").count() == len(sent)


def test_consent_rechecked_at_send_time(pilot):
    priya = _consent("C")
    run = svc.generate(pilot, OCT)
    assert run.statements.get(unit__number="C").channel == Statement.Channel.EMAIL
    _revoke(priya)
    owner_b = _consent("B")  # consents after generation
    assert run.statements.get(unit__number="B").channel == Statement.Channel.MAIL
    _on(pilot)
    with capture_jobs() as queue:
        svc.send_run(run)
        queue.run_all()
    c = run.statements.get(unit__number="C")
    b = run.statements.get(unit__number="B")
    assert c.channel == Statement.Channel.MAIL
    assert not _email_deliveries(c).exists()
    assert c.letter_id in set(c.mail_batch.items.values_list("letter_id", flat=True))
    assert b.channel == Statement.Channel.EMAIL
    assert b.mail_batch is None
    assert not b.letter.mail_items.exists()
    assert Notification.objects.get(
        recipient=owner_b, type_key=svc.NOTIFICATION_TYPE
    ).deliveries.get().status == (Delivery.Status.SENT)


def test_send_is_idempotent_and_rerun_keeps_sent(pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    _on(pilot)
    with capture_jobs() as queue:
        svc.send_run(run)
        queue.run_all()
        assert svc.send_run(run) == []
        c = run.statements.get(unit__number="C")
        svc.send_statement(c)
        assert queue.run_all() == 0
    assert _email_deliveries(c).count() == 1
    assert MailBatch.objects.count() == 1
    svc.generate(pilot, OCT)
    assert run.statements.get(unit__number="C").pk == c.pk


def test_single_unit_send_touches_only_that_unit(pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    _on(pilot)
    b = run.statements.get(unit__number="B")
    draft = b.mail_batch
    others = draft.items.count() - 1
    svc.send_statement(b)
    b.refresh_from_db()
    assert b.status == Statement.Status.SENT and b.mail_batch.items.count() == 1
    draft.refresh_from_db()
    assert draft.status == MailBatch.Status.DRAFT and draft.items.count() == others
    assert run.statements.filter(status=Statement.Status.SENT).count() == 1
    assert [d.status for d in _email_deliveries(run.statements.get(unit__number="C"))] == [
        Delivery.Status.PREVIEW
    ]
    # Sending the rest reuses the remaining draft batch.
    svc.send_run(run)
    assert run.statements.exclude(status=Statement.Status.SENT).count() == 0
    draft.refresh_from_db()
    assert draft.status != MailBatch.Status.DRAFT


def test_bounce_raises_todo_and_counts_toward_revoke(pilot):
    priya = _consent("C")
    run1 = svc.generate(pilot, OCT)
    _on(pilot)
    with capture_jobs() as queue:
        svc.send_run(run1)
        queue.run_all()
    c = run1.statements.get(unit__number="C")
    d = _email_deliveries(c).get()
    bounces.mark_bounced(pilot, d.provider_id, "550")
    d.refresh_from_db()
    assert d.status == Delivery.Status.BOUNCED
    titles = [i.title for i in dashboard_todos.collect(pilot)]
    assert "Email to unit C bounced" in titles
    run2 = svc.generate(pilot, dt.date(2026, 11, 1))
    with capture_jobs() as queue:
        svc.send_run(run2)
        queue.run_all()
    d2 = _email_deliveries(run2.statements.get(unit__number="C")).get()
    bounces.mark_bounced(pilot, d2.provider_id, "550")
    assert not reg.current_notice_preference(priya).e_delivery_consent


def _login(client, email):
    client.force_login(User.objects.get(email=email))


def test_views_gating_and_permission(client, pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    c = run.statements.get(unit__number="C")
    _login(client, TREASURER)
    page = client.get(f"/money/statements/{run.pk}/").content.decode()
    assert 'data-testid="send-statements" disabled' in page
    unit_page = client.get(f"/units/{c.unit.number}/")
    assert unit_page.status_code == 200
    assert 'data-testid="send-statement" disabled' in unit_page.content.decode()
    resp = client.post(f"/money/statements/{run.pk}/send/")
    assert resp.status_code == 400
    assert client.post(f"/money/statements/statement/{c.pk}/send/").status_code == 400
    _on(pilot)
    page = client.get(f"/money/statements/{run.pk}/").content.decode()
    assert 'data-testid="send-statements" disabled' not in page
    with capture_jobs():
        resp = client.post(f"/money/statements/statement/{c.pk}/send/", {"next": "/units/C/"})
    assert resp.status_code == 302 and resp["Location"] == "/units/C/"
    c.refresh_from_db()
    assert c.status == Statement.Status.SENT
    client.logout()
    _login(client, "ownerc@cedarrow.example")
    assert client.post(f"/money/statements/{run.pk}/send/").status_code == 403


def test_resident_sees_statement_only_once_sent(client, pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    _on(pilot)
    _login(client, "ownerc@cedarrow.example")
    page = client.get("/me/statements/").content.decode()
    c = run.statements.get(unit__number="C")
    url = f"/me/statements/{c.pk}/pdf/"
    assert url not in page
    with capture_jobs():
        svc.send_statement(c)
    assert url in client.get("/me/statements/").content.decode()
