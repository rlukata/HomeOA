# ruff: noqa: F811  (pytest fixtures imported from test_statutory_notices)
"""Statutory notices wait for an approved translation of the owner's language."""

import datetime as dt
import io

import pytest
from django.contrib.auth import get_user_model
from pypdf import PdfReader

from homeoa.access.testing import grant, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.dashboard import todos
from homeoa.letters import pdf
from homeoa.letters.letterhead import letterhead
from homeoa.letters.models import LetterTemplate, LetterTranslation
from homeoa.letters.services import ensure_default_templates, letter_bytes, upload_translation
from homeoa.notify.models import Notification
from homeoa.registry.models import Person

from .test_statutory_notices import action, assoc, day, pack, steps  # noqa: F401

SPANISH = "Aviso de morosidad (traduccion aprobada por la junta)."


def _spanish_owner():
    Person.objects.filter(full_name="Owner G").update(preferred_language="es")


def _template(assoc):
    ensure_default_templates(assoc)
    return LetterTemplate.objects.get(key="wa-delinquency-notice")


def _text(letter) -> str:
    reader = PdfReader(io.BytesIO(letter_bytes(letter)))
    return "\n".join(p.extract_text() for p in reader.pages)


def test_english_owner_is_not_held(assoc):
    result = delinquency_run(assoc, day(1))
    assert not result.held
    assert steps() == ["first_notice"]


def delinquency_run(assoc, on):
    from homeoa.statements import delinquency

    return delinquency.run(assoc, on)


def test_hold_until_text_translation_then_release(assoc, settings):
    _spanish_owner()
    settings.HOMEOA_FIXED_TODAY = day(1).isoformat()
    result = delinquency_run(assoc, day(1))
    assert [c.unit.number for c in result.held] == ["G"]
    assert steps() == []
    item = next(i for i in todos.collect(assoc) if i.id.startswith("delinquency-translation"))
    assert "Spanish" in str(item.title)
    assert item.due == day(30)  # the 30-day deadline does not move
    assert day(30).isoformat() in str(item.sub)
    # The first-notice deadline to-do stays too.
    assert any(i.id.startswith("delinquency-first-notice") for i in todos.collect(assoc))

    upload_translation(_template(assoc), "es", text=SPANISH, note="Translator X")
    assert AuditEvent.objects.filter(action="letter.translation_upload").exists()
    result = delinquency_run(assoc, day(2))
    assert not result.held
    first = action("first_notice")
    body = _text(first.letter)
    assert "NOTICE" in body.upper()
    assert "traduccion aprobada" in body
    assert not [i for i in todos.collect(assoc) if i.id.startswith("delinquency-translation")]
    email = Notification.objects.filter(type_key="statements.delinquency_notice").first()
    assert SPANISH in email.body
    assert email.attachments == []  # text translations stay in the body


def test_pdf_translation_is_appended(assoc):
    _spanish_owner()
    assert delinquency_run(assoc, day(1)).held
    extra = pdf.build_pdf(
        letterhead=letterhead(assoc),
        address_lines=[],
        date_text="",
        subject="Aviso",
        body="Pagina traducida",
        footer_note="",
        title="es",
    )
    upload_translation(_template(assoc), "es", pdf_bytes=extra, filename="aviso.pdf")
    delinquency_run(assoc, day(2))
    letter = action("first_notice").letter
    reader = PdfReader(io.BytesIO(letter_bytes(letter)))
    assert "Pagina traducida" in reader.pages[-1].extract_text()
    email = Notification.objects.filter(type_key="statements.delinquency_notice").first()
    assert "enclosed with the mailed notice" not in email.body
    [spec] = email.attachments
    assert spec["name"] == "aviso.pdf" and spec["content_type"] == "application/pdf"
    assert LetterTranslation.objects.get().file_key == spec["file_key"]  # a key, not bytes

    msg, delivery = _send_copy(assoc, email)
    assert delivery.status == "sent"
    [(name, content, mime)] = msg.attachments
    assert (name, mime) == ("aviso.pdf", "application/pdf")
    assert content == extra
    assert "Pagina traducida" not in msg.body  # the mailed letter is unchanged; PDF attached


def _send_copy(assoc, note):
    """Send the stored preview email copy now through the email channel."""
    from django.core import mail

    from homeoa.notify.channels import email as email_channel

    open_residents(assoc)
    delivery = note.deliveries.get(channel="email")
    delivery.status = "queued"
    delivery.save()
    mail.outbox.clear()
    email_channel.deliver(delivery)
    delivery.refresh_from_db()
    return (mail.outbox[0] if mail.outbox else None), delivery


def test_missing_pdf_fails_copy_without_counting_toward_consent(assoc):
    from homeoa.notify.consent import consecutive_failures
    from homeoa.providers import get_provider

    _spanish_owner()
    delinquency_run(assoc, day(1))
    extra = pdf.build_pdf(
        letterhead=letterhead(assoc),
        address_lines=[],
        date_text="",
        subject="Aviso",
        body="Pagina traducida",
        footer_note="",
        title="es",
    )
    upload_translation(_template(assoc), "es", pdf_bytes=extra, filename="aviso.pdf")
    delinquency_run(assoc, day(2))
    email = Notification.objects.filter(type_key="statements.delinquency_notice").first()
    get_provider("FileStorage", assoc).delete(email.attachments[0]["file_key"])
    msg, delivery = _send_copy(assoc, email)
    assert msg is None
    assert delivery.status == "failed" and delivery.failure_kind == "attachment"
    assert consecutive_failures(email.recipient, delivery.created_at) == 0
    item = next(i for i in todos.collect(assoc) if i.id == f"notify-failed-{delivery.pk}")
    assert "attachment" in str(item.title).lower()


def test_upload_rejects_empty_and_bad_pdf(assoc):
    from homeoa.letters.services import TranslationError

    with pytest.raises(TranslationError):
        upload_translation(_template(assoc), "es")
    with pytest.raises(TranslationError):
        upload_translation(_template(assoc), "es", pdf_bytes=b"not a pdf")
    assert not LetterTranslation.objects.exists()


@pytest.fixture
def board(client, assoc):
    user = get_user_model().objects.create_user(email="president@example.org")
    grant(user, association=assoc, title="president", start=dt.date(2026, 1, 1))
    client.force_login(user)
    return client


def test_board_edits_language_and_uploads_translation(board, assoc, settings):
    settings.HOMEOA_FIXED_TODAY = day(1).isoformat()
    person = Person.objects.get(full_name="Owner G")
    resp = board.post(
        f"/units/people/{person.pk}/edit/",
        {"full_name": "Owner G", "email": "g@ex.org", "preferred_language": "es"},
    )
    assert resp.status_code == 302
    person.refresh_from_db()
    assert person.preferred_language == "es"
    assert AuditEvent.objects.filter(
        object_id=str(person.pk), changes__has_key="preferred_language"
    ).exists()
    assert "Spanish" in board.get(f"/units/people/{person.pk}/").content.decode()

    delinquency_run(assoc, day(1))
    page = board.get("/money/delinquency/").content.decode()
    assert "Waiting for translation" in page

    template = _template(assoc)
    resp = board.post(
        f"/settings/letters/{template.pk}/translations/", {"language": "es", "text": SPANISH}
    )
    assert resp.status_code == 400  # approval box not ticked
    resp = board.post(
        f"/settings/letters/{template.pk}/translations/",
        {"language": "es", "text": SPANISH, "approved": "on"},
    )
    assert resp.status_code == 302
    assert LetterTranslation.objects.get().text == SPANISH
    assert "Waiting for translation" not in board.get("/money/delinquency/").content.decode()
