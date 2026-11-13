"""Retention guard: what was sent or posted cannot be deleted by code."""

import datetime as dt

import pytest
from django.db import transaction
from django.db.models import ProtectedError

from homeoa.access.testing import open_residents
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.core.retention import RetentionError, assert_deletable_files
from homeoa.jobs.testing import capture_jobs
from homeoa.letters.models import Letter, MailBatch, MailItem
from homeoa.notify.models import Delivery, Notification
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Unit
from homeoa.statements import owner_statements as svc
from homeoa.statements.models import DelinquencyAction, DelinquencyCase, Statement, StatementRun

OCT = dt.date(2026, 10, 1)


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


@pytest.fixture
def sent_run(pilot):
    owner = reg.current_owners(Unit.objects.get(number="C"))[0]
    owner.email = "ownerc@example.com"
    owner.save()
    reg.record_notice_preference(
        owner, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )
    run = svc.generate(pilot, OCT)
    open_residents(pilot)
    with capture_jobs() as queue:
        svc.send_run(run)
        queue.run_all()
    return run


def _blocked(fn):
    # Django deletes without a savepoint, so give the failed delete its own.
    with pytest.raises(RetentionError), transaction.atomic():
        fn()


def test_sent_statement_cannot_be_deleted(sent_run):
    c = sent_run.statements.get(unit__number="C")
    _blocked(c.delete)
    _blocked(Statement.objects.filter(pk=c.pk).delete)
    _blocked(StatementRun.objects.filter(pk=sent_run.pk).delete)  # cascade is guarded too
    assert Statement.objects.filter(pk=c.pk).exists()


def test_sent_batch_and_its_letters_cannot_be_deleted(sent_run):
    batch = MailBatch.objects.exclude(status=MailBatch.Status.DRAFT).get()
    _blocked(batch.delete)
    _blocked(MailBatch.objects.filter(pk=batch.pk).delete)
    letter = batch.items.first().letter
    # The FK from the mail item also protects it; the guard is the second line.
    with pytest.raises((RetentionError, ProtectedError)), transaction.atomic():
        letter.delete()
    from homeoa.core.retention import letter_block

    assert letter_block(letter)
    assert Letter.objects.filter(pk=letter.pk).exists()


def test_sent_notification_and_delivery_cannot_be_deleted(sent_run):
    c = sent_run.statements.get(unit__number="C")
    note = Notification.objects.get(object_key=svc.nsvc.object_key_for(c))
    delivery = note.deliveries.get()
    assert delivery.status != Delivery.Status.PREVIEW
    _blocked(delivery.delete)
    _blocked(Delivery.objects.filter(pk=delivery.pk).delete)
    _blocked(note.delete)
    _blocked(Notification.objects.filter(pk=note.pk).delete)


def test_letter_with_sent_delivery_cannot_be_deleted(sent_run, pilot):
    c = sent_run.statements.get(unit__number="C")
    delivery = Notification.objects.get(object_key=svc.nsvc.object_key_for(c)).deliveries.get()
    draft = MailBatch.objects.create(name="draft")
    item = MailItem.objects.create(batch=draft, letter=c.letter)
    item.deliveries.add(delivery)
    from homeoa.core.retention import letter_block

    assert letter_block(c.letter) == "letter has a sent delivery"
    with pytest.raises((RetentionError, ProtectedError)), transaction.atomic():
        Letter.objects.filter(pk=c.letter_id).delete()


def test_letter_with_delinquency_action_cannot_be_deleted(pilot):
    run = svc.generate(pilot, OCT)
    s = run.statements.first()
    case = DelinquencyCase.objects.create(unit=s.unit, anchor_due_date=OCT, opened_on=OCT)
    DelinquencyAction.objects.create(
        case=case,
        step=DelinquencyAction._meta.get_field("step").choices[0][0],
        as_of=OCT,
        letter=s.letter,
    )
    with pytest.raises((RetentionError, ProtectedError)), transaction.atomic():
        Letter.objects.filter(pk=s.letter_id).delete()


def test_files_of_kept_records_are_never_deleted(sent_run, pilot):
    batch = MailBatch.objects.exclude(status=MailBatch.Status.DRAFT).get()
    c = sent_run.statements.get(unit__number="C")
    _blocked(lambda: assert_deletable_files(pilot, [batch.pdf_key]))
    _blocked(lambda: assert_deletable_files(pilot, [c.letter.file_key]))
    _blocked(lambda: svc._delete_files_on_commit(pilot, [batch.manifest_key]))
    assert_deletable_files(pilot, ["never-referenced.pdf"])


def test_rerun_still_replaces_unsent_and_keeps_sent(sent_run, pilot):
    c = sent_run.statements.get(unit__number="C")
    sent_ids = set(sent_run.statements.values_list("pk", flat=True))
    svc.generate(pilot, OCT)  # re-run deletes only drafts and previews
    assert set(sent_run.statements.values_list("pk", flat=True)) == sent_ids
    assert Statement.objects.get(pk=c.pk).status == Statement.Status.SENT


def test_rerun_of_unsent_run_deletes_drafts(pilot):
    run = svc.generate(pilot, OCT)
    first = set(run.statements.values_list("pk", flat=True))
    svc.generate(pilot, OCT)
    assert not first & set(run.statements.values_list("pk", flat=True))
