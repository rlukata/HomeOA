"""E-notice consent."""

import re

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access import invitations
from homeoa.access.testing import open_residents
from homeoa.audit.models import AuditEvent
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.jobs.testing import capture_jobs
from homeoa.notify import bounces, types
from homeoa.notify import services as notify_services
from homeoa.notify import todos as notify_todos
from homeoa.notify.consent import consecutive_failures
from homeoa.notify.models import Delivery
from homeoa.plugins import registry as plugin_registry
from homeoa.registry import consent as consent_text
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Person

User = get_user_model()
C_EMAIL = "ownerc@cedarrow.example"
A_EMAIL = "ownera@cedarrow.example"
BOARD_EMAIL = "treasurer@cedarrow.example"

NOTICE = types.NotificationType(
    key="test.legal_notice",
    label="Legal notice",
    default_channels=("email",),
    mandatory=True,
    subject_template="Notice {{ n }}",
    body_template="Body",
)


@pytest.fixture(autouse=True)
def registered():
    types.register(NOTICE)
    yield
    types.unregister(NOTICE.key)


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = "2026-10-07"
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


def person(email):
    return Person.objects.get(user__email=email)


def consent(p, source=NoticePreference.Source.PORTAL):
    return reg.record_notice_preference(
        p,
        NoticePreference.Delivery.EMAIL,
        e_delivery_consent=True,
        source=source,
        consent_text_version=consent_text.VERSION,
    )


def send(p, n):
    with capture_jobs() as queue:
        [note] = notify_services.notify(NOTICE.key, [p], {"n": n}, period=str(n))
        queue.run_all()
    return note.deliveries.get()


def fail_smtp(monkeypatch, fail=True):
    from homeoa.providers.defaults.smtp import SmtpEmail
    from homeoa.providers.email import EmailResult

    if fail:
        monkeypatch.setattr(
            SmtpEmail,
            "send",
            lambda self, m: EmailResult(sent=False, error="mailbox full", failure_kind="recipient"),
        )
    else:
        monkeypatch.undo()


# ------------------------------------------------------------------ seed and routing


def test_seed_consents_from_portal_for_e_delivery_units(pilot):
    a = reg.current_notice_preference(person(A_EMAIL))
    assert a.valid_consent and a.source == NoticePreference.Source.PORTAL
    assert a.email == A_EMAIL and a.consent_text_version == consent_text.VERSION
    assert reg.current_notice_preference(person(C_EMAIL)) is None
    assert notify_services.email_allowed(person(A_EMAIL))
    assert not notify_services.email_allowed(person(C_EMAIL))


def test_board_or_import_source_cannot_consent(pilot):
    p = person(C_EMAIL)
    for source in (NoticePreference.Source.BOARD, NoticePreference.Source.IMPORT):
        with pytest.raises(ValidationError):
            consent(p, source)
    with pytest.raises(ValidationError):  # paper without its scan
        consent(p, NoticePreference.Source.PAPER)


def test_consent_names_the_address(pilot):
    p = person(A_EMAIL)
    assert notify_services.email_allowed(p)
    p.email = "new@example.com"
    p.save()
    assert not notify_services.email_allowed(p)  # consent named the old address
    assert notify_services.channels_for(NOTICE, p) == ["postal"]


# ------------------------------------------------------------------ imports


def test_import_never_consents(pilot):
    p = person(C_EMAIL)
    engine = type("E", (), {})()
    engine.user = None
    engine.report = type("R", (), {"add": lambda *a, **k: None, "warnings": []})()
    from homeoa.imports import engine as engine_mod

    engine_mod.Applier._notice(engine, p, {"e_delivery": True})
    pref = reg.current_notice_preference(p)
    assert pref.delivery == NoticePreference.Delivery.POSTAL
    assert not pref.e_delivery_consent and pref.note == "Imported e-delivery: yes"
    assert pref.source == NoticePreference.Source.IMPORT
    assert not notify_services.email_allowed(p)
    # Re-importing the same flag adds nothing; HomeOA consent is never overwritten.
    engine_mod.Applier._notice(engine, p, {"e_delivery": True})
    assert p.notice_preferences.count() == 1
    consent(p)
    engine_mod.Applier._notice(engine, p, {"e_delivery": False})
    assert reg.current_notice_preference(p).valid_consent


def test_migration_revokes_imported_consents(pilot):
    from importlib import import_module

    mig = import_module("homeoa.registry.migrations.0004_enotice_consent")
    p = person(C_EMAIL)
    NoticePreference.objects.create(
        association=pilot,
        person=p,
        delivery="email",
        e_delivery_consent=True,
        email=C_EMAIL,
        source="import",
    )
    mig.revoke_imported_consents(django_apps, None)
    pref = reg.current_notice_preference(p)
    assert pref.revoke_reason == "import" and pref.delivery == "postal"
    assert pref.note == "AppFolio e-delivery: yes"
    # A's portal consent is untouched.
    assert reg.current_notice_preference(person(A_EMAIL)).valid_consent
    items = [i for i in notify_todos.todo_items(pilot) if i["id"] == "notify-consent-imported"]
    assert len(items) == 1 and "Priya Raman" in items[0]["sub"]
    consent(p)  # the owner consents again: the to-do goes away
    assert not [i for i in notify_todos.todo_items(pilot) if i["id"] == "notify-consent-imported"]


# ------------------------------------------------------------------ auto-revoke


def test_two_failures_revoke_consent(pilot, monkeypatch):
    p = person(A_EMAIL)
    fail_smtp(monkeypatch)
    assert send(p, 1).status == Delivery.Status.FAILED
    assert reg.current_notice_preference(p).valid_consent  # one failure is not enough
    assert send(p, 2).status == Delivery.Status.FAILED
    pref = reg.current_notice_preference(p)
    assert pref.revoke_reason == NoticePreference.RevokeReason.FAILURES
    assert pref.revoked_at is not None and pref.delivery == "postal"
    assert AuditEvent.objects.filter(action="notice_preference.consent_revoked").exists()
    assert notify_services.channels_for(NOTICE, p) == ["postal"]  # falls back to mail
    ids = [i["id"] for i in notify_todos.todo_items(pilot)]
    assert f"notify-consent-revoked-{pref.pk}" in ids
    # The owner consents again: the to-do resolves and the count starts over.
    consent(p)
    assert f"notify-consent-revoked-{pref.pk}" not in [
        i["id"] for i in notify_todos.todo_items(pilot)
    ]
    assert consecutive_failures(p, reg.current_notice_preference(p).recorded_at) == 0


def test_success_resets_the_count(pilot, monkeypatch):
    p = person(A_EMAIL)
    fail_smtp(monkeypatch)
    send(p, 1)
    fail_smtp(monkeypatch, fail=False)
    assert send(p, 2).status == Delivery.Status.SENT
    fail_smtp(monkeypatch)
    send(p, 3)
    assert reg.current_notice_preference(p).valid_consent
    send(p, 4)
    assert not reg.current_notice_preference(p).valid_consent


def test_bounces_count_as_failures(pilot):
    p = person(A_EMAIL)
    for n in (1, 2):
        d = send(p, n)
        assert d.status == Delivery.Status.SENT
        bounces.mark_bounced(pilot, d.provider_id, "no such user")
    assert reg.current_notice_preference(p).revoke_reason == "failures"


def test_delinquency_email_copy_unaffected_by_revocation(pilot):
    p = person(A_EMAIL)
    reg.revoke_consent(p, NoticePreference.RevokeReason.FAILURES)
    with capture_jobs():
        [note] = notify_services.notify(
            NOTICE.key, [p], {"n": 9}, period="x", force_channels=("email", "postal")
        )
    assert sorted(note.deliveries.values_list("channel", flat=True)) == ["email", "postal"]


def test_statement_routing_flips_to_mail(pilot):
    from homeoa.statements import owner_statements  # noqa: F401 - uses email_allowed

    p = person(A_EMAIL)
    assert notify_services.channels_for(NOTICE, p) == ["email"]
    reg.revoke_consent(p, NoticePreference.RevokeReason.OWNER)
    assert notify_services.channels_for(NOTICE, p) == ["postal"]


# ------------------------------------------------------------------ portal


def test_portal_give_needs_address_and_version(client, pilot):
    client.force_login(User.objects.get(email=C_EMAIL))
    p = person(C_EMAIL)
    base = {"action": "give", "agree": "yes", "email": C_EMAIL}
    client.post("/me/profile/consent/", {**base, "version": "old"})
    assert reg.current_notice_preference(p) is None
    client.post("/me/profile/consent/", {**base, "email": "x@y.z", "version": consent_text.VERSION})
    assert reg.current_notice_preference(p) is None
    client.post("/me/profile/consent/", {**base, "version": consent_text.VERSION})
    pref = reg.current_notice_preference(p)
    assert pref.valid_consent and pref.consent_text_version == consent_text.VERSION
    assert AuditEvent.objects.filter(action="notice_preference.consent_given").exists()
    client.post("/me/profile/consent/", {"action": "withdraw"})
    latest = reg.current_notice_preference(p)
    assert latest.revoke_reason == NoticePreference.RevokeReason.OWNER
    assert not notify_services.email_allowed(p)  # immediate


def test_first_login_prompt_records_invite_source(client, pilot):
    client.force_login(User.objects.get(email=C_EMAIL))
    html = client.get("/me/").content.decode()
    assert 'data-testid="consent-prompt"' in html and consent_text.VERSION in html
    assert "attorney review" in html
    resp = client.post(
        "/me/profile/consent/",
        {
            "action": "give",
            "agree": "yes",
            "context": "invite",
            "email": C_EMAIL,
            "version": consent_text.VERSION,
        },
    )
    assert resp.url == "/me/"
    pref = reg.current_notice_preference(person(C_EMAIL))
    assert pref.source == NoticePreference.Source.INVITE and pref.valid_consent
    assert 'data-testid="consent-prompt"' not in client.get("/me/").content.decode()


def test_prompt_keep_mail_stops_asking(client, pilot):
    client.force_login(User.objects.get(email=C_EMAIL))
    client.post("/me/profile/consent/", {"action": "keep_mail", "context": "invite"})
    pref = reg.current_notice_preference(person(C_EMAIL))
    assert pref.delivery == "postal" and pref.source == NoticePreference.Source.INVITE
    assert 'data-testid="consent-prompt"' not in client.get("/me/").content.decode()


def test_invitation_email_needs_no_consent(pilot):
    p = person(C_EMAIL)
    assert not notify_services.email_allowed(p)
    inv = invitations.draft_invitation(p)
    with capture_jobs():
        notes = invitations.send_invitation(inv)
    assert [d.channel for n in notes for d in n.deliveries.all()] == ["email"]


# ------------------------------------------------------------------ board paper consent


def board_client(client):
    client.force_login(User.objects.get(email=BOARD_EMAIL))
    return client


def test_board_paper_consent_with_scan(client, pilot):
    c = board_client(client)
    p = person(C_EMAIL)
    url = f"/units/people/{p.pk}/notices/"
    c.post(url, {"delivery": "email", "source": "board", "note": ""})
    assert reg.current_notice_preference(p) is None
    c.post(url, {"delivery": "email", "source": "paper", "note": ""})
    assert reg.current_notice_preference(p) is None
    scan = SimpleUploadedFile("signed.pdf", b"%PDF-1.4 signed", content_type="application/pdf")
    c.post(url, {"delivery": "email", "source": "paper", "note": "", "evidence": scan})
    pref = reg.current_notice_preference(p)
    assert pref.valid_consent and pref.source == "paper" and pref.evidence_key
    page = c.get(f"/units/people/{p.pk}/").content.decode()
    evidence_url = f"/units/people/{p.pk}/notices/{pref.pk}/evidence/"
    assert evidence_url in page
    resp = c.get(evidence_url)
    assert resp.status_code == 200 and b"".join(resp) == b"%PDF-1.4 signed"
    assert AuditEvent.objects.filter(action="notice_preference.consent_given").exists()


def test_person_page_unit_links_are_inline(client, pilot):
    c = board_client(client)
    html = c.get(f"/units/people/{person(C_EMAIL).pk}/").content.decode()
    assert re.search(r'<a class="inline-link" href="/units/C/"', html)


def test_letter_edit_page_has_unique_ids(client, pilot):
    from homeoa.letters.models import LetterTemplate

    c = board_client(client)
    template = LetterTemplate.objects.order_by("pk").first()
    html = c.get(f"/settings/letters/{template.pk}/").content.decode()
    ids = re.findall(r'\sid="([^"]+)"', html)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, dupes
