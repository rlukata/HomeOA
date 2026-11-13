"""Unit file v1 (DOC-02) and statement re-run cleanup."""

import datetime as dt

import pytest
from django.contrib.auth import get_user_model

from homeoa.access import permissions as perms
from homeoa.access import services as access
from homeoa.access.testing import open_residents
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.documents import unitfile
from homeoa.letters import services as letter_services
from homeoa.letters.models import Letter, LetterTemplate
from homeoa.plugins import registry
from homeoa.providers import get_provider
from homeoa.registry.models import Unit
from homeoa.statements import owner_statements as svc

User = get_user_model()
OCT = dt.date(2026, 10, 1)
BOARD = "treasurer@cedarrow.example"


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


def _login(client, email):
    client.force_login(User.objects.get(email=email))


def _general_letter(assoc, unit):
    letter_services.ensure_default_templates(assoc)
    template = LetterTemplate.objects.exclude(key=svc.TEMPLATE_KEY).first()
    return letter_services.generate_letter(template, unit, as_of=dt.date(2026, 9, 15))


def test_unit_file_lists_every_contributor_by_date(client, pilot):
    unit = Unit.objects.get(number="C")
    letter = _general_letter(pilot, unit)
    run = svc.generate(pilot, OCT)
    statement = run.statements.get(unit=unit)

    _login(client, BOARD)
    resp = client.get("/units/C/file/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'data-kind="ledger"' in html and "#ledger-h" in html
    assert f"/settings/letters/letter/{letter.pk}.pdf" in html
    assert f"/money/statements/pdf/{statement.pk}.pdf" in html
    assert 'data-kind="comms"' in html
    # The statement PDF is listed once, as a statement, not again as a letter.
    assert f"/settings/letters/letter/{statement.letter_id}.pdf" not in html

    # Newest first.
    from django.test import RequestFactory

    req = RequestFactory().get("/")
    req.user = User.objects.get(email=BOARD)
    req.association = pilot
    dates = [e.date for e in unitfile.entries(unit, req)]
    assert dates == sorted(dates, reverse=True)
    assert {e.kind for e in unitfile.entries(unit, req)} >= {"ledger", "letters", "statements"}

    only = client.get("/units/C/file/?kind=letters").content.decode()
    assert 'data-kind="letters"' in only and 'data-kind="statements"' not in only


def test_unit_page_links_to_file(client, pilot):
    _login(client, BOARD)
    assert b"/units/C/file/" in client.get("/units/C/").content


def test_registered_contributor_appears(client, pilot):
    def extra(unit, request):
        return [unitfile.Entry(dt.date(2026, 1, 2), "test", "Test", f"Note for {unit.number}")]

    unitfile.register("test", extra)
    try:
        _login(client, BOARD)
        assert b"Note for C" in client.get("/units/C/file/").content
    finally:
        unitfile.unregister("test")


def test_board_only(client, pilot):
    open_residents(pilot)
    _login(client, "ownerc@cedarrow.example")
    assert client.get("/units/C/file/").status_code == 403
    assert b"/units/C/file/" not in client.get("/units/C/").content
    client.logout()
    assert client.get("/units/C/file/").status_code in (302, 403)


def test_contributors_gate_on_their_own_permissions(client, pilot):
    """CPA (finance read only): ledger and statements, no letters or comm log; no page."""
    from django.test import RequestFactory

    unit = Unit.objects.get(number="C")
    _general_letter(pilot, unit)
    svc.generate(pilot, OCT)
    user = User.objects.create_user("cpa2@example.com")
    access.assign_role(user, pilot, perms.CPA)
    req = RequestFactory().get("/")
    req.user = user
    req.association = pilot
    assert {e.kind for e in unitfile.entries(unit, req)} == {"ledger", "statements"}
    client.force_login(user)
    assert client.get("/units/C/file/").status_code == 403


# --- statements re-run cleanup ----------------------------------------------------------------


def test_rerun_deletes_replaced_letters_and_files(pilot):
    run = svc.generate(pilot, OCT)
    old = list(Letter.objects.filter(template_version__template__key=svc.TEMPLATE_KEY))
    assert len(old) == 9
    storage = get_provider("FileStorage", pilot)
    run2 = svc.generate(pilot, OCT)
    assert run2.pk == run.pk
    now = Letter.objects.filter(template_version__template__key=svc.TEMPLATE_KEY)
    assert now.count() == 9
    assert not {x.pk for x in old} & set(now.values_list("pk", flat=True))
    assert all(storage.exists(x.file_key) for x in now)


def test_rerun_removes_old_files_after_commit(pilot, django_capture_on_commit_callbacks):
    svc.generate(pilot, OCT)
    old = list(Letter.objects.filter(template_version__template__key=svc.TEMPLATE_KEY))
    storage = get_provider("FileStorage", pilot)
    with django_capture_on_commit_callbacks(execute=True):
        svc.generate(pilot, OCT)
    assert not any(storage.exists(x.file_key) for x in old)
