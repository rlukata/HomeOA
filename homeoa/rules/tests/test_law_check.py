"""The yearly law check: the citation index, change sources, the stored review list, and
how the System admin hears about it. Nothing here reaches the network."""

import datetime as dt
import urllib.request
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from homeoa_rulepack_us_wa import lawwatch as wa
from homeoa_rulepack_us_wa.pack import pack as wa_pack

from homeoa import providers
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.jobs.scheduler import _registry
from homeoa.notify.models import Delivery, Notification
from homeoa.notify.services import notify
from homeoa.plugins import services
from homeoa.registry.models import Person
from homeoa.rules import citations, lawcheck, lawwatch
from homeoa.rules.models import LawCheck
from homeoa.rules.tasks import law_check

User = get_user_model()
RUN_ON = dt.date(2027, 9, 1)
FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "rcw_cite_affected_sample.xml"
SESSION_LAW = FIXTURES / "session_law_sample.xml"
NO_SESSION_LAW = FIXTURES / "session_law_none_sample.xml"
URL = "/settings/rules/law-changes/"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def assoc(db, settings):
    settings.HOMEOA_FIXED_TODAY = RUN_ON.isoformat()
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


@pytest.fixture
def wa_assoc(assoc):
    services.save_installation(assoc, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(assoc, "RulePack", "rulepack_us_wa")
    return assoc


@pytest.fixture
def admin(assoc):
    usr = User.objects.create_user("admin@example.org", full_name="Ada Admin")
    grant(usr, "sys_admin", association=assoc)
    return usr


@pytest.fixture
def board(assoc):
    usr = User.objects.create_user("board@example.org", full_name="Bo Board")
    grant(usr, association=assoc)
    return usr


def _use(monkeypatch, source):
    monkeypatch.setattr(lawwatch, "source_for", lambda pack: source)


CHANGE = {"bill": "SB 9991", "session_law": "2027 c 1", "action": "amended"}


# ---------------------------------------------------------------- citations


def test_a_single_citation_with_subsections_gives_its_section():
    assert citations.parse("RCW 64.34.364(17)(b)").sections == ("64.34.364",)


def test_a_citation_list_gives_every_section():
    assert citations.parse("RCW 64.90.513, 64.90.580").sections == ("64.90.513", "64.90.580")
    assert citations.parse("RCW 64.34.425; RCW 64.90.640").sections == ("64.34.425", "64.90.640")


def test_federal_and_irs_citations_are_kept_apart():
    parsed = citations.parse("26 U.S.C. 528(b); RCW 64.90.065")
    assert parsed.sections == ("64.90.065",)
    assert parsed.other == ("26 U.S.C. 528(b)",)


def test_citations_inside_a_note_are_found():
    parsed = citations.parse(
        "Late fees are capped by RCW 64.90.485(1) and 12 CFR 1005.10(d)(1) applies.",
        free_text=True,
    )
    assert parsed.sections == ("64.90.485",)
    assert parsed.other == ("12 CFR 1005.10(d)(1)",)


def test_the_wa_index_watches_the_sections_that_decide_which_law_applies():
    index = citations.index(wa_pack)
    for section in ("64.34.076", "64.90.360", "64.90.365"):
        assert citations.Place("pack", "applies_under") in index.sections[section], section


def test_the_wa_index_finds_the_late_fee_question_and_rule_values_citing_64_34_364():
    index = citations.index(wa_pack)
    places = index.sections["64.34.364"]
    assert citations.Place("setup", "late_fees.flat_amount") in places
    assert citations.Place("value", "late_fees.cap") in places
    for section in ("64.90.640", "64.34.372", "64.90.525"):
        assert section in index.sections
    assert any(c.startswith("26 U.S.C. 528") for c in index.other)


def test_the_wa_pack_records_when_its_law_was_checked_and_its_change_source():
    assert wa_pack.law_checked_through == dt.date(2026, 10, 11)
    assert isinstance(lawwatch.source_for(wa_pack), wa.RcwCiteAffectedSource)


# ---------------------------------------------------------------- the yearly job


def test_the_yearly_job_runs_on_september_first_without_catch_up():
    job = _registry["rules.law_check"]
    assert not job.catch_up
    tz = dt.UTC
    assert job.is_due(dt.date(2027, 9, 1), tz)
    assert not job.is_due(dt.date(2027, 9, 2), tz)


def test_the_job_records_changes_found_with_the_pack_places_that_cite_them(
    wa_assoc, admin, monkeypatch
):
    source = lawwatch.FakeChangeSource({"64.34.364": [CHANGE]})
    _use(monkeypatch, source)
    law_check(wa_assoc, RUN_ON)
    check = LawCheck.objects.get()
    assert check.status == LawCheck.Status.CHANGES
    assert check.pack_id == "us_wa_condo" and check.pack_version
    assert check.checked_through == dt.date(2026, 10, 11)
    assert source.calls[0][1] == dt.date(2026, 10, 11)
    [finding] = check.findings
    assert finding["section"] == "64.34.364"
    assert finding["changes"][0]["session_law"] == "2027 c 1"
    keys = {p["key"] for p in finding["places"]}
    assert {"late_fees.cap", "late_fees.flat_amount"} <= keys
    assert any(i["citation"].startswith("26 U.S.C.") for i in check.not_checked)


def test_the_job_records_no_changes(wa_assoc, monkeypatch):
    _use(monkeypatch, lawwatch.FakeChangeSource({}))
    law_check(wa_assoc, RUN_ON)
    check = LawCheck.objects.get()
    assert check.status == LawCheck.Status.NO_CHANGES
    assert not check.needs_review


def test_a_network_failure_records_could_not_check_with_the_reason(wa_assoc, monkeypatch):
    _use(monkeypatch, lawwatch.FakeChangeSource(error="service down"))
    law_check(wa_assoc, RUN_ON)
    check = LawCheck.objects.get()
    assert check.status == LawCheck.Status.FAILED
    assert check.reason == "service down"


def test_a_pack_without_a_change_source_records_that_it_cannot_be_checked(assoc):
    law_check(assoc, RUN_ON)
    assert LawCheck.objects.get().status == LawCheck.Status.NO_SOURCE


# ---------------------------------------------------------------- telling the admin


def test_the_to_do_shows_for_the_system_admin_not_a_board_member_and_clears_when_reviewed(
    client, wa_assoc, admin, board, monkeypatch
):
    _use(monkeypatch, lawwatch.FakeChangeSource({"64.34.364": [CHANGE]}))
    law_check(wa_assoc, RUN_ON)
    titles = [i.title for i in todos.collect(wa_assoc, admin)]
    assert "The law changed: 1 section the state pack relies on. Review the list." in titles
    assert not [i for i in todos.collect(wa_assoc, board) if i.source == "law_check"]

    client.force_login(admin)
    check = LawCheck.objects.get()
    response = client.post(f"/settings/rules/law-changes/{check.pk}/reviewed/")
    assert response.status_code == 302
    assert not [i for i in todos.collect(wa_assoc, admin) if i.source == "law_check"]
    assert AuditEvent.objects.filter(action="rules.law_check_reviewed").exists()


def test_a_board_member_without_settings_power_cannot_open_or_review(
    client, wa_assoc, board, monkeypatch
):
    _use(monkeypatch, lawwatch.FakeChangeSource({"64.34.364": [CHANGE]}))
    law_check(wa_assoc, RUN_ON)
    client.force_login(board)
    assert client.get(URL).status_code == 403
    check = LawCheck.objects.get()
    assert client.post(f"/settings/rules/law-changes/{check.pk}/reviewed/").status_code == 403
    assert URL.encode() not in client.get("/settings/").content


def test_the_page_shows_the_review_list_and_the_citations_not_checked(
    client, wa_assoc, admin, monkeypatch
):
    _use(monkeypatch, lawwatch.FakeChangeSource({"64.34.364": [CHANGE]}))
    law_check(wa_assoc, RUN_ON)
    client.force_login(admin)
    assert URL in client.get("/settings/").content.decode()
    page = client.get(URL).content.decode()
    assert "Law checked through Oct 11, 2026." in page
    assert "RCW 64.34.364" in page and "SB 9991" in page and "2027 c 1" in page
    assert "late_fees.cap" in page
    assert "26 U.S.C. 528" in page
    assert "Mark reviewed" in page


def _law_mail(assoc):
    return Notification.all_associations.filter(association=assoc, type_key=lawcheck.NOTIFY_TYPE)


def test_the_email_goes_to_system_admins_when_changes_are_found(
    wa_assoc, admin, board, monkeypatch
):
    _use(monkeypatch, lawwatch.FakeChangeSource({"64.34.364": [CHANGE]}))
    law_check(wa_assoc, RUN_ON)
    mails = _law_mail(wa_assoc)
    assert [m.recipient.user_id for m in mails] == [admin.pk]
    assert "64.34.364" in mails[0].body


def test_before_go_live_a_system_admin_who_is_not_on_the_board_still_gets_the_email(
    wa_assoc, admin, monkeypatch
):
    _use(monkeypatch, lawwatch.FakeChangeSource({"64.34.364": [CHANGE]}))
    law_check(wa_assoc, RUN_ON)
    statuses = {d.status for d in _law_mail(wa_assoc).get().deliveries.all()}
    assert statuses and Delivery.Status.PREVIEW not in statuses


def test_before_go_live_an_owner_with_no_roles_is_still_held_as_a_preview(wa_assoc, admin):
    owner = Person.objects.create(
        full_name="Olive Owner", email="olive@example.org", association=wa_assoc
    )
    note = notify(
        lawcheck.NOTIFY_TYPE, [owner], {}, association=wa_assoc, force_channels=["inapp"]
    )[0]
    assert {d.status for d in note.deliveries.all()} == {Delivery.Status.PREVIEW}


def test_preview_only_still_holds_the_email_for_a_system_admin(wa_assoc, admin):
    person = lawcheck.admin_recipients(wa_assoc)[0]
    note = notify(lawcheck.NOTIFY_TYPE, [person], {}, association=wa_assoc, preview_only=True)[0]
    assert {d.status for d in note.deliveries.all()} == {Delivery.Status.PREVIEW}


def test_the_email_goes_out_when_the_check_fails(wa_assoc, admin, monkeypatch):
    _use(monkeypatch, lawwatch.FakeChangeSource(error="service down"))
    law_check(wa_assoc, RUN_ON)
    assert "service down" in _law_mail(wa_assoc).get().body


def test_no_email_when_nothing_changed(wa_assoc, admin, monkeypatch):
    _use(monkeypatch, lawwatch.FakeChangeSource({}))
    law_check(wa_assoc, RUN_ON)
    assert not _law_mail(wa_assoc).exists()


# ---------------------------------------------------------------- the WA source


def test_the_wa_source_asks_each_biennium_from_since_to_now():
    assert wa.biennium(2026) == "2025-26"
    assert wa.biennium(2027) == "2027-28"
    assert wa.bienniums(dt.date(2026, 10, 11), dt.date(2029, 3, 1)) == [
        "2025-26",
        "2027-28",
        "2029-30",
    ]


def _wa_source(seen):
    def opener(request):
        seen.append(request)
        if request.full_url == wa.SESSION_LAW_URL:
            passed = "<billNumber>9991</billNumber>" in request.data.decode()
            return (SESSION_LAW if passed else NO_SESSION_LAW).read_bytes()
        return SAMPLE.read_bytes()

    return wa.RcwCiteAffectedSource(opener=opener, today=dt.date(2027, 9, 1))


def test_the_wa_source_asks_the_cite_service_per_biennium():
    seen = []
    _wa_source(seen).affected(["64.34.364"], dt.date(2026, 10, 11))
    cites = [r for r in seen if r.full_url == wa.URL]
    assert len(cites) == 2  # 2025-26 and 2027-28
    assert (
        cites[0].get_header("Soapaction")
        == '"http://WSLWebServices.leg.wa.gov/GetLegislationAffectingRcwCite"'
    )
    assert "<biennium>2025-26</biennium><rcwCite>64.34.364</rcwCite>" in cites[0].data.decode()
    assert "<biennium>2027-28</biennium>" in cites[1].data.decode()


def test_a_kept_bill_shows_its_session_law_and_effective_date():
    changes = _wa_source([]).affected(["64.34.364"], dt.date(2026, 10, 11))
    assert changes[0] == lawwatch.LawChange(
        section="64.34.364",
        bill="ESSB 9991 (2025-26)",
        session_law="2025 c 119",
        effective=dt.date(2025, 7, 27),
        url="https://app.leg.wa.gov/billsummary?BillNumber=9991&Year=2025",
    )


def test_a_bill_without_a_session_law_is_dropped():
    changes = _wa_source([]).affected(["64.34.364"], dt.date(2026, 10, 11))
    assert {c.bill for c in changes} == {"ESSB 9991 (2025-26)"}


def test_each_bill_is_looked_up_once_per_run():
    seen = []
    changes = _wa_source(seen).affected(["64.34.364", "64.90.485"], dt.date(2026, 10, 11))
    lookups = [r for r in seen if r.full_url == wa.SESSION_LAW_URL]
    assert len(lookups) == 2  # bills 9991 and 9992, despite four cite replies naming them
    assert (
        lookups[0].get_header("Soapaction")
        == '"http://WSLWebServices.leg.wa.gov/GetSessionLawByBill"'
    )
    assert "<biennium>2025-26</biennium><billNumber>9991</billNumber>" in lookups[0].data.decode()
    assert len(changes) == 4  # the passed bill, per section, per cite reply


def test_a_soap_fault_is_a_plain_source_error():
    fault = (
        b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        b"<soap:Fault><faultcode>soap:Server</faultcode><faultstring>Invalid biennium"
        b"</faultstring></soap:Fault></soap:Body></soap:Envelope>"
    )
    with pytest.raises(lawwatch.LawSourceError, match="Invalid biennium"):
        wa.parse_response("64.34.364", fault)


def test_the_wa_source_refuses_the_network_under_test_settings():
    with pytest.raises(lawwatch.LawSourceError, match="turned off"):
        wa.RcwCiteAffectedSource().affected(["64.34.364"], dt.date(2026, 10, 11))


# ---------------------------------------------------------------- the command


def test_the_command_refuses_a_live_check_under_test_settings(wa_assoc):
    with pytest.raises(CommandError, match="--fake"):
        call_command("law_check", association=wa_assoc.pk)


def test_the_command_prints_the_review_list_with_a_fake_and_stores_nothing(wa_assoc):
    out = StringIO()
    call_command("law_check", association=wa_assoc.pk, fake=True, stdout=out)
    text = out.getvalue()
    assert "Changes found" in text and "SAMPLE 0000" in text and "cited by" in text
    assert not LawCheck.objects.exists()


def test_any_other_fault_from_the_session_law_service_is_an_error():
    fault = NO_SESSION_LAW.read_bytes().replace(
        b"No session law data found for BillNumber: [6299], Biennium: [2025-26].",
        b"Server was unable to process request.",
    )
    with pytest.raises(lawwatch.LawSourceError):
        wa.parse_session_law(fault)


def test_the_no_session_law_fault_means_the_bill_did_not_become_law():
    assert wa.parse_session_law(NO_SESSION_LAW.read_bytes()) is None
