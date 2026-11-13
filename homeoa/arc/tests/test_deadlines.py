# ruff: noqa: F811 (pytest fixture imported from test_arc)
"""ARC decision deadlines, reminders, overdue to-do and decision letters."""

import datetime as dt
from unittest import mock

import pytest
from django.db import transaction
from django.urls import reverse

from homeoa import rules
from homeoa.arc import deadlines, decision_letters, services
from homeoa.arc.models import ArcRequest
from homeoa.arc.todos import overdue_items
from homeoa.audit.models import AuditEvent
from homeoa.letters.models import Letter, MailBatch
from homeoa.notify.models import Notification

from .test_arc import assoc, board, make_request, owner  # noqa: F401

D = dt.date
TODAY = "homeoa.arc.services.association_today"


def at(day):
    return mock.patch(TODAY, return_value=day)


def submit_on(assoc, person, day, **kw):
    with at(day):
        return make_request(assoc, person, **kw)


# ------------------------------------------------------------------ deadline


def test_decide_by_from_generic_pack(assoc):
    _, p = owner(assoc)
    req = submit_on(assoc, p, D(2026, 10, 1))
    assert rules.resolve(assoc, "arc.decision_days").verified is False
    assert req.clock_started_on == D(2026, 10, 1)
    assert req.decide_by == D(2026, 10, 31)


def test_decide_by_from_board_override(assoc):
    rules.set_override(assoc, "arc.decision_days", 45, reason="Declaration §8.3")
    _, p = owner(assoc)
    req = submit_on(assoc, p, D(2026, 10, 1))
    assert req.decide_by == D(2026, 11, 15)


def test_decide_by_reset_when_info_received(assoc):
    usr, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = submit_on(assoc, p, D(2026, 10, 1))
    with at(D(2026, 10, 5)):
        services.request_info(req, message="Paint sample?", user=b_usr)
    req.refresh_from_db()
    assert req.decide_by == D(2026, 10, 31)  # unchanged while waiting
    with at(D(2026, 10, 12)):
        services.provide_info(req, message="Attached", user=usr)
    req.refresh_from_db()
    assert req.clock_started_on == D(2026, 10, 12)
    assert req.decide_by == D(2026, 11, 11)
    # a plain message from the owner does not restart it
    with at(D(2026, 10, 20)):
        services.provide_info(req, message="Any news?", user=usr)
    req.refresh_from_db()
    assert req.decide_by == D(2026, 11, 11)


# ------------------------------------------------------------------ reminders


def test_reminders_sent_once_per_offset(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    board(assoc, "b1@example.org", "president")
    board(assoc, "b2@example.org")
    submit_on(assoc, p, D(2026, 10, 1))  # decide_by Oct 31

    def run(day):
        with django_capture_on_commit_callbacks(execute=True):
            deadlines.send_reminders(assoc, day)
        return Notification.objects.filter(type_key=deadlines.REMINDER).count()

    assert run(D(2026, 10, 23)) == 0  # 8 days before
    assert run(D(2026, 10, 24)) == 2  # 7 days: one per reviewer
    assert run(D(2026, 10, 24)) == 2  # rerun: idempotent
    assert run(D(2026, 10, 25)) == 2  # still the 7-day reminder
    assert run(D(2026, 10, 30)) == 4  # 1 day before
    assert run(D(2026, 10, 31)) == 4
    assert run(D(2026, 11, 1)) == 4  # past: overdue to-do instead
    periods = set(
        Notification.objects.filter(type_key=deadlines.REMINDER).values_list("period", flat=True)
    )
    assert periods == {"2026-10-31:7", "2026-10-31:1"}


def test_reminder_emails_reviewer_without_consent(assoc, django_capture_on_commit_callbacks):
    from homeoa.notify import channels as ch
    from homeoa.notify.models import Delivery

    _, p = owner(assoc)
    _, rev = board(assoc, "b1@example.org", "president")
    submit_on(assoc, p, D(2026, 10, 1))
    with django_capture_on_commit_callbacks(execute=True):
        deadlines.send_reminders(assoc, D(2026, 10, 24))
    note = Notification.objects.get(type_key=deadlines.REMINDER, recipient=rev)
    assert Delivery.objects.filter(notification=note, channel=ch.EMAIL).exists()


def test_no_reminders_while_waiting_on_owner(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "b1@example.org", "president")
    req = submit_on(assoc, p, D(2026, 10, 1))
    with at(D(2026, 10, 2)), django_capture_on_commit_callbacks(execute=True):
        services.request_info(req, message="More?", user=b_usr)
    with django_capture_on_commit_callbacks(execute=True):
        deadlines.send_reminders(assoc, D(2026, 10, 30))
    assert not Notification.objects.filter(type_key=deadlines.REMINDER).exists()


def test_reminder_job_registered():
    from homeoa.jobs.scheduler import periodic_jobs

    assert "arc.deadline_reminders" in periodic_jobs()


# ------------------------------------------------------------------ overdue to-do


def test_overdue_todo_with_fixed_clock(assoc):
    _, p = owner(assoc)
    req = submit_on(assoc, p, D(2026, 10, 1))
    with mock.patch("homeoa.arc.todos.association_today", return_value=D(2026, 10, 31)):
        assert overdue_items(assoc) == []
    with mock.patch("homeoa.arc.todos.association_today", return_value=D(2026, 11, 1)):
        items = overdue_items(assoc)
    assert [i["id"] for i in items] == [f"arc-overdue-{req.pk}"]
    assert items[0]["due"] == D(2026, 10, 31)
    assert "A" in items[0]["title"]


def test_overdue_page_and_deemed_note(assoc, client):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = submit_on(assoc, p, D(2020, 1, 1))
    client.force_login(b_usr)
    html = client.get(reverse("arc:detail", args=[req.pk])).content.decode()
    assert "Overdue" in html and 'data-testid="deemed-note"' not in html
    rules.set_override(assoc, "arc.deemed_approved", True, reason="Declaration")
    html = client.get(reverse("arc:detail", args=[req.pk])).content.decode()
    assert 'data-testid="deemed-note"' in html
    req.refresh_from_db()
    assert req.status == ArcRequest.Status.SUBMITTED  # never auto-approved


# ------------------------------------------------------------------ letters


def give_consent(person):
    from homeoa.registry import services as reg
    from homeoa.registry.models import NoticePreference

    reg.record_notice_preference(
        person, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )


def decide(req, user, outcome, django_capture_on_commit_callbacks, **kw):
    with django_capture_on_commit_callbacks(execute=True):
        services.decide(req, outcome=outcome, user=user, **kw)
    req.refresh_from_db()
    return req


@pytest.mark.parametrize(
    ("outcome", "key", "words"),
    [
        ("approved", "arc-approval", "approved your architectural request"),
        ("approved_with_conditions", "arc-approval-conditions", "Use Sherwin-Williams 6244"),
        ("denied", "arc-denial", "denied your architectural request"),
    ],
)
def test_letter_per_outcome_on_unit_file(
    assoc, client, outcome, key, words, django_capture_on_commit_callbacks
):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = make_request(assoc, p)
    req = decide(
        req,
        b_usr,
        outcome,
        django_capture_on_commit_callbacks,
        conditions="Use Sherwin-Williams 6244",
        citations="Declaration §8.2",
    )
    letter = req.decision_letter
    assert letter is not None and letter.unit.number == "A"
    assert letter.template_version.template.key == key
    from homeoa.letters import render

    v = letter.template_version
    from homeoa.letters.services import context_for

    _subj, body = render.render_letter(
        v.subject, v.body, context_for(assoc, letter.unit, **decision_letters.merge_values(req))
    )
    assert words in body and "Declaration §8.2" in body
    assert AuditEvent.objects.filter(action="arc.decision_letter").exists()
    # on the unit file (core letters contributor)
    client.force_login(b_usr)
    html = client.get(reverse("documents:unit_file", args=["A"])).content.decode()
    assert letter.subject in html


def test_email_copy_only_with_fresh_consent(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = make_request(assoc, p)
    decide(req, b_usr, "approved", django_capture_on_commit_callbacks)
    assert not Notification.objects.filter(type_key=decision_letters.DECIDED).exists()
    assert MailBatch.objects.count() == 1  # no consent: mailed

    _, p2 = owner(assoc, "B", "owner-b@example.org")
    give_consent(p2)
    req2 = make_request(assoc, p2, n="B")
    decide(req2, b_usr, "denied", django_capture_on_commit_callbacks)
    note = Notification.objects.get(type_key=decision_letters.DECIDED)
    assert note.recipient == p2
    assert note.attachments[0]["file_key"] == req2.decision_letter.file_key
    assert MailBatch.objects.count() == 1  # no batch for the consenting owner


def test_rollback_sends_nothing(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    give_consent(p)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = make_request(assoc, p)
    with django_capture_on_commit_callbacks(execute=True) as cbs:
        try:
            with transaction.atomic():
                services.decide(req, outcome="approved", user=b_usr)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    assert cbs == []
    assert not Letter.objects.exists()
    assert not Notification.objects.filter(type_key=decision_letters.DECIDED).exists()
    req.refresh_from_db()
    assert req.status == ArcRequest.Status.SUBMITTED


def test_letter_failure_is_audited_not_raised(assoc, django_capture_on_commit_callbacks):
    _, p = owner(assoc)
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = make_request(assoc, p)
    with mock.patch(
        "homeoa.letters.services.generate_letter", side_effect=RuntimeError("disk full")
    ):
        req = decide(req, b_usr, "approved", django_capture_on_commit_callbacks)
    assert req.status == ArcRequest.Status.APPROVED
    assert req.decision_letter is None
    assert AuditEvent.objects.filter(action="arc.decision_letter_failed").exists()


def test_owner_downloads_own_letter(assoc, client, django_capture_on_commit_callbacks):
    usr, p = owner(assoc)
    other, _ = owner(assoc, "B", "owner-b@example.org")
    b_usr, _ = board(assoc, "chair@example.org", "president")
    req = make_request(assoc, p)
    decide(req, b_usr, "approved", django_capture_on_commit_callbacks)
    url = reverse("arc:mine_letter", args=[req.pk])
    client.force_login(usr)
    resp = client.get(url)
    assert resp.status_code == 200 and resp["Content-Type"] == "application/pdf"
    client.force_login(other)
    assert client.get(url).status_code == 404
