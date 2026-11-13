# ruff: noqa: F811  (pytest fixtures imported from test_statutory_notices)
"""The board releases the statute's email copy of a delinquency notice."""

import datetime as dt

from django.contrib.auth import get_user_model
from django.core import mail

from homeoa.access.testing import close_residents, grant, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.jobs.testing import capture_jobs
from homeoa.letters import pdf
from homeoa.letters.letterhead import letterhead
from homeoa.letters.models import LetterTemplate
from homeoa.letters.services import ensure_default_templates, upload_translation
from homeoa.notify.models import Delivery
from homeoa.registry.models import Person
from homeoa.statements import delinquency

from .test_statutory_notices import action, assoc, board_client, day, pack  # noqa: F401


def _residents(assoc, on=True):
    if on:
        open_residents(assoc)
    else:
        close_residents(assoc)


def _url(a):
    return f"/money/delinquency/action/{a.pk}/email-copy/"


def _email():
    return Delivery.objects.get(channel="email")


def _with_pdf(assoc):
    Person.objects.filter(full_name="Owner G").update(preferred_language="es")
    delinquency.run(assoc, day(1))
    ensure_default_templates(assoc)
    extra = pdf.build_pdf(
        letterhead=letterhead(assoc),
        address_lines=[],
        date_text="",
        subject="Aviso",
        body="Pagina traducida",
        footer_note="",
        title="es",
    )
    upload_translation(
        LetterTemplate.objects.get(key="wa-delinquency-notice"),
        "es",
        pdf_bytes=extra,
        filename="aviso.pdf",
    )
    delinquency.run(assoc, day(2))
    return extra


def test_release_sends_once_with_attachment_and_audits(board_client, assoc):
    extra = _with_pdf(assoc)
    _residents(assoc)
    first = action("first_notice")
    page = board_client.get("/money/delinquency/").content.decode()
    assert 'data-testid="send-email-copy"' in page
    with capture_jobs() as queue:
        assert board_client.post(_url(first)).status_code == 302
        assert board_client.post(_url(first)).status_code == 302  # idempotent
        queue.run_all()
    [msg] = mail.outbox
    assert msg.to == ["g@ex.org"]
    [(name, content, _mime)] = msg.attachments
    assert name == "aviso.pdf" and content == extra
    assert _email().status == Delivery.Status.SENT
    assert AuditEvent.objects.filter(action="delinquency.email_copy_released").count() == 1
    page = board_client.get("/money/delinquency/").content.decode()
    assert 'data-testid="send-email-copy"' not in page
    assert "Email sent" in page


def test_non_manage_user_forbidden(client, assoc):
    delinquency.run(assoc, day(1))
    user = get_user_model().objects.create_user(email="member@example.org")
    grant(user, association=assoc, start=dt.date(2026, 1, 1))
    client.force_login(user)
    assert client.post(_url(action("first_notice"))).status_code == 403
    assert _email().status == Delivery.Status.PREVIEW


def test_residents_off_stays_preview(board_client, assoc):
    delinquency.run(assoc, day(1))
    page = board_client.get("/money/delinquency/").content.decode()
    assert "disabled" in page and "Residents are off" in page
    result = delinquency.release_email_copy(action("first_notice"))
    assert result.gated and not result.sent
    assert _email().status == Delivery.Status.PREVIEW
    assert not AuditEvent.objects.filter(action="delinquency.email_copy_released").exists()


def test_missing_address_reported(assoc):
    delinquency.run(assoc, day(1))
    _residents(assoc)
    Person.objects.filter(full_name="Owner G").update(email="")
    result = delinquency.release_email_copy(action("first_notice"))
    assert [str(p) for p in result.no_email] and not result.sent
    assert _email().status == Delivery.Status.PREVIEW


def test_no_email_copy_no_button(board_client, assoc):
    Person.objects.filter(full_name="Owner G").update(email="")
    delinquency.run(assoc, day(1))
    page = board_client.get("/money/delinquency/").content.decode()
    assert 'data-testid="send-email-copy"' not in page
