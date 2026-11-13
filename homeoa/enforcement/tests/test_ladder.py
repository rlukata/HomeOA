"""Escalation ladder, notices and the cure-deadline to-do (VIO-03)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import transaction
from django.urls import reverse
from homeoa_rulepack_us_wa.pack import pack as wa_pack

from homeoa import rules
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.documents import unitfile
from homeoa.enforcement import ladder
from homeoa.enforcement.models import (
    FineSchedule,
    FineScheduleLine,
    Violation,
    ViolationCategory,
    ViolationStep,
)
from homeoa.enforcement.services import EnforcementError, log_violation
from homeoa.letters.models import Letter, MailBatch
from homeoa.meetings.tests.test_meetings import TODAY, User, _consent, pilot  # noqa: F401
from homeoa.notify.models import Notification
from homeoa.registry.models import Unit
from homeoa.registry.services import current_owners
from homeoa.rules.base import Basis

D = dt.date
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
COURTESY = ViolationStep.Kind.COURTESY
FORMAL = ViolationStep.Kind.FORMAL


def _use_pack(monkeypatch, pack):
    monkeypatch.setattr(rules, "active_pack", lambda association: pack)


def _violation(a, number="A"):
    cat = ViolationCategory.objects.get_or_create(
        association=a, name="Pets", defaults={"rule_citation": "Rules §4.2"}
    )[0]
    return log_violation(
        a,
        unit=Unit.objects.get(number=number),
        category=cat,
        observed_on=D(2026, 9, 1),
        description="Dog off leash in the lobby",
        photos=[("dog.png", PNG)],
    )


def _deliveries(v):
    return Notification.objects.filter(type_key=ladder.NOTIFICATION_TYPE, unit=v.unit)


# --- packs ------------------------------------------------------------------------------


def test_packs_define_keys_unverified():
    from homeoa.rules.packs.generic import pack as generic

    for pack in (generic, wa_pack):
        for key in (
            "enforcement.courtesy_notice_required",
            "enforcement.cure_days",
            "enforcement.hearing_notice_min_days",
        ):
            rv = pack.get(key)
            assert rv.value is not None and not rv.verified and rv.basis == Basis.POLICY
    assert "RCW" in wa_pack.get("enforcement.cure_days").citation
    assert "RCW" not in generic.get("enforcement.cure_days").citation


@pytest.mark.parametrize("which", ["generic", "wa"])
def test_cure_by_from_pack(pilot, monkeypatch, which):  # noqa: F811
    from homeoa.rules.packs.generic import pack as generic

    pack = generic if which == "generic" else wa_pack
    _use_pack(monkeypatch, pack)
    days = pack.get("enforcement.cure_days").value
    v = _violation(pilot)
    ladder.send_notice(v, COURTESY)
    step = ladder.send_notice(v, FORMAL)
    assert step.cure_by == TODAY + dt.timedelta(days=days)
    assert ladder.cure_days(pilot, TODAY).citation == pack.get("enforcement.cure_days").citation


def test_cure_by_follows_override(pilot):  # noqa: F811
    rules.set_override(pilot, "enforcement.cure_days", 30, reason="Bylaws 7.1")
    v = _violation(pilot)
    ladder.send_notice(v, COURTESY)
    assert ladder.send_notice(v, FORMAL).cure_by == TODAY + dt.timedelta(days=30)


# --- order ------------------------------------------------------------------------------


def test_formal_requires_courtesy_when_pack_says(pilot):  # noqa: F811
    v = _violation(pilot)
    with pytest.raises(EnforcementError, match="courtesy"):
        ladder.send_notice(v, FORMAL)
    ladder.send_notice(v, COURTESY)
    with pytest.raises(EnforcementError, match="already"):
        ladder.send_notice(v, COURTESY)
    ladder.send_notice(v, FORMAL)
    with pytest.raises(EnforcementError):
        ladder.send_notice(v, FORMAL)
    assert list(v.steps.values_list("kind", flat=True)) == [COURTESY, FORMAL]


def test_formal_without_courtesy_when_not_required(pilot):  # noqa: F811
    rules.set_override(pilot, "enforcement.courtesy_notice_required", False, reason="Bylaws")
    v = _violation(pilot)
    ladder.send_notice(v, FORMAL)
    with pytest.raises(EnforcementError, match="formal"):
        ladder.send_notice(v, COURTESY)


def test_closed_violation_gets_no_notice(pilot):  # noqa: F811
    v = _violation(pilot)
    ladder.mark_cured(v, cured_on=D(2026, 10, 1))
    v.refresh_from_db()
    assert v.status == Violation.Status.CURED and v.cured_on == D(2026, 10, 1)
    with pytest.raises(EnforcementError, match="open"):
        ladder.send_notice(v, COURTESY)
    with pytest.raises(EnforcementError):
        ladder.mark_cured(v)
    assert AuditEvent.objects.filter(action="enforcement.cured").exists()


def test_cure_date_not_before_observed(pilot):  # noqa: F811
    v = _violation(pilot)
    with pytest.raises(EnforcementError):
        ladder.mark_cured(v, cured_on=D(2026, 8, 1))


# --- letters and delivery ---------------------------------------------------------------


def test_letter_generated_with_merge_fields_and_on_unit_file(pilot):  # noqa: F811
    schedule = FineSchedule.objects.create(
        association=pilot, version=1, effective_on=D(2026, 1, 1), adopted_on=D(2026, 1, 1)
    )
    v = _violation(pilot, "C")  # owner C has an email but no e-notice consent
    FineScheduleLine.objects.create(
        association=pilot,
        schedule=schedule,
        category=v.category,
        first_amount=Decimal("50.00"),
        repeat_amount=Decimal("100.00"),
    )
    ladder.send_notice(v, COURTESY)
    step = ladder.send_notice(v, FORMAL)
    letter = step.letter
    assert Letter.objects.filter(unit=v.unit).count() == 2
    extra = ladder.merge_extra(v, step.cure_by)
    assert extra["violation_fine_amount"] == "$50.00"
    assert extra["violation_citation"] == "Rules §4.2"
    assert "October 21, 2026" in extra["violation_cure_by"]
    assert letter.subject.startswith("Notice of violation")
    # No consent: letter goes by mail (draft batch), no email.
    assert step.delivery == ViolationStep.Delivery.LETTER
    assert step.batch is not None and step.batch.status == MailBatch.Status.DRAFT
    assert step.emailed == 0
    assert not _deliveries(v).exists()
    # Unit file lists the letters (board with settings access).
    board = User.objects.create_user(email="b@example.com", password="x-pass-12345")
    grant(board, association=pilot)

    class Req:
        user = board
        association = pilot

    entries = unitfile.entries(v.unit, Req())
    assert sum(1 for e in entries if e.kind == "letters") >= 2
    ev = AuditEvent.objects.filter(action="enforcement.notice_sent").latest("pk")
    assert ev.changes["kind"] == FORMAL


def test_email_only_with_fresh_consent(pilot):  # noqa: F811
    owners = current_owners(Unit.objects.get(number="A"), TODAY)
    for o in owners:
        _consent_person(o)
    v = _violation(pilot)
    step = ladder.send_notice(v, COURTESY)
    assert step.delivery == ViolationStep.Delivery.EMAIL
    assert step.batch is None
    notes = _deliveries(v)
    assert {n.recipient_id for n in notes} == {o.pk for o in owners}
    # Comm log (deliveries filed on the unit)
    from homeoa.notify import commlog

    assert commlog.entries(v.unit).exists()


def test_owner_without_consent_gets_no_email(pilot):  # noqa: F811
    owner = current_owners(Unit.objects.get(number="B"), TODAY)[0]
    owner.email = "noconsent@example.com"
    owner.save()
    v = _violation(pilot, "B")
    step = ladder.send_notice(v, COURTESY)
    assert step.delivery == ViolationStep.Delivery.LETTER
    assert not _deliveries(v).exists()


def _consent_person(person):
    from homeoa.registry import services as reg
    from homeoa.registry.models import NoticePreference

    person.email = person.email or f"p{person.pk}@example.com"
    person.save()
    reg.record_notice_preference(
        person, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )


@pytest.mark.deferred_notify
def test_no_email_on_rollback(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v = _violation(pilot, "C")
    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        ladder.send_notice(v, COURTESY)
        raise RuntimeError("boom")
    assert callbacks == []
    assert not ViolationStep.objects.exists()
    assert not _deliveries(v).exists()


@pytest.mark.deferred_notify
def test_email_waits_for_commit(pilot, django_capture_on_commit_callbacks):  # noqa: F811
    _consent("C")
    v = _violation(pilot, "C")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        ladder.send_notice(v, COURTESY)
        assert not _deliveries(v).exists()
    assert callbacks
    for fn in callbacks:
        fn()
    assert _deliveries(v).exists()


# --- to-do ------------------------------------------------------------------------------


def test_todo_after_cure_by(pilot, settings):  # noqa: F811
    from homeoa.enforcement.todos import todo_items

    v = _violation(pilot)
    ladder.send_notice(v, COURTESY)
    step = ladder.send_notice(v, FORMAL)

    def titles():
        return [i["title"] for i in todo_items(pilot)]

    msg = "Cure deadline passed: schedule hearing"
    settings.HOMEOA_FIXED_TODAY = step.cure_by.isoformat()
    assert msg not in titles()
    settings.HOMEOA_FIXED_TODAY = (step.cure_by + dt.timedelta(days=1)).isoformat()
    assert msg in titles()
    ladder.mark_cured(v)
    assert msg not in titles()


# --- views ------------------------------------------------------------------------------


def test_views_send_and_cure(pilot, client):  # noqa: F811
    board = User.objects.create_user(email="b2@example.com", password="x-pass-12345")
    grant(board, association=pilot)
    client.force_login(board)
    v = _violation(pilot)
    page = client.get(reverse("enforcement:detail", args=[v.pk]))
    assert page.status_code == 200 and b"send-courtesy" in page.content
    r = client.post(reverse("enforcement:notice", args=[v.pk]), {"kind": FORMAL})
    assert r.status_code == 302 and not v.steps.exists()
    client.post(reverse("enforcement:notice", args=[v.pk]), {"kind": COURTESY})
    assert v.steps.filter(kind=COURTESY).exists()
    client.post(reverse("enforcement:cured", args=[v.pk]), {"cured_on": "2026-10-05"})
    v.refresh_from_db()
    assert v.status == Violation.Status.CURED


def test_views_need_permission(pilot, client):  # noqa: F811
    owner = User.objects.create_user(email="o@example.com", password="x-pass-12345")
    client.force_login(owner)
    v = _violation(pilot)
    r = client.post(reverse("enforcement:notice", args=[v.pk]), {"kind": COURTESY})
    assert r.status_code in (302, 403) and not v.steps.exists()


def test_status_form_cured_sets_cured_on(pilot, client):  # noqa: F811
    board = User.objects.create_user(email="b3@example.com", password="x-pass-12345")
    grant(board, association=pilot)
    client.force_login(board)
    v = _violation(pilot)
    client.post(reverse("enforcement:status", args=[v.pk]), {"status": "cured"})
    v.refresh_from_db()
    assert v.status == Violation.Status.CURED and v.cured_on == TODAY
    assert not Violation.objects.filter(status="cured", cured_on__isnull=True).exists()
    assert AuditEvent.objects.filter(action="enforcement.cured").exists()
