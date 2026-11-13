"""The delinquency ladder's statutory notices (RCW 64.34.364(17)-(18) shaped).

Rule values come from a test pack shaped like the WA pack (the real WA values live in the WA pack);
nothing here is a statement of law.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.db.models import ProtectedError

from homeoa import rules
from homeoa.access.testing import grant
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import default_schedule_kwargs, generate_assessments
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.letters import statutory
from homeoa.letters.models import Letter, MailBatch
from homeoa.letters.services import letter_bytes
from homeoa.notify.models import Delivery, Notification
from homeoa.registry.models import Person, Unit
from homeoa.registry.services import set_initial_ownership
from homeoa.rules import Basis, RulePack, RuleValue
from homeoa.statements import delinquency
from homeoa.statements.models import (
    DelinquencyAction,
    DelinquencyCase,
    PreforeclosureRecord,
    ProtectedRecordError,
)

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"
DUE = dt.date(2026, 9, 10)
OLD, NEW = "wa_64_34_364_17a_iii", "wa_64_90_485_21a_iii"


def day(n: int) -> dt.date:
    return DUE + dt.timedelta(days=n)


def _v(value, **kw):
    return RuleValue(value=value, basis=Basis.STATUTE, **kw)


TEST_PACK = RulePack(
    id="test_statutory",
    name="Test statutory pack",
    jurisdiction="US-XX",
    values={
        "delinquency.notice.statutory_text": [
            _v(OLD, effective_to=dt.date(2027, 12, 31)),
            _v(NEW, effective_from=dt.date(2028, 1, 1)),
        ],
        "delinquency.first_notice.max_days_past_due": _v(30),
        "delinquency.standstill_days": _v(15),
        "delinquency.second_notice.min_days_past_due": _v(90),
        "delinquency.second_notice.min_days_after_first": _v(60),
        "delinquency.foreclosure.min_months_assessments": _v(3),
        "delinquency.foreclosure.min_amount": _v("2000.00"),
        "delinquency.foreclosure.min_days_after_threshold": _v(90),
        "delinquency.notice.mail_required": _v(True),
        "delinquency.notice.email_copy_if_known": _v(True),
    },
)


@pytest.fixture
def pack(monkeypatch):
    monkeypatch.setattr(rules, "active_pack", lambda association: TEST_PACK)


def months(start: dt.date, n: int) -> list[dt.date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(dt.date(y, m, 1))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


@pytest.fixture
def assoc(db, settings, tmp_path, pack):
    from homeoa.ledger.chart import load_chart_of_accounts
    from homeoa.payments.services import record_receipt

    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        for number, pct in (("A", D("50")), ("G", D("50"))):
            unit = Unit.objects.create(
                association=a,
                number=number,
                allocated_interest=pct,
                address_line1=f"1 Cedar Row {number}",
                city="Tukwila",
                region="WA",
                postal_code="98188",
            )
            person = Person.objects.create(
                association=a,
                full_name=f"Owner {number}",
                email=f"{number.lower()}@ex.org",
                # G lives elsewhere: notices go to the unit and to this address.
                address_line1="9 Elsewhere Ave" if number == "G" else "",
                city="Seattle" if number == "G" else "",
                region="WA" if number == "G" else "",
                postal_code="98101" if number == "G" else "",
            )
            set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
        s = AssessmentSchedule.objects.create(
            association=a,
            name="Dues",
            annual_amount=D("8400.00"),  # G owes $350.00 a month
            effective_start=dt.date(2026, 1, 1),
            **{**default_schedule_kwargs(a), "due_day": 10},
        )
        unit_a = Unit.objects.get(number="A")
        for period in months(dt.date(2026, 9, 1), 9):
            generate_assessments(s, period)
            charge = Charge.objects.get(unit=unit_a, period=period)
            record_receipt(unit=unit_a, amount=charge.amount, received_date=period, method="check")
        yield a


def action(step):
    return DelinquencyAction.objects.get(case__unit__number="G", step=step)


def steps():
    case = DelinquencyCase.objects.get(unit__number="G", status="open")
    return list(case.actions.values_list("step", flat=True))


def run_days(assoc, start, end):
    for n in range(start, end + 1):
        delinquency.run(assoc, day(n))


def test_verbatim_blocks_match_the_statute_fixtures():
    for text_id in (OLD, NEW):
        stored = statutory.get(text_id).text
        assert stored == (FIXTURES / f"{text_id}.txt").read_text(encoding="utf-8")
        assert stored.startswith("THIS IS A NOTICE OF DELINQUENCY FOR PAST DUE ASSESSMENTS\n")
        assert len([x for x in stored.splitlines() if statutory.CONTACT_LINE.match(x)]) == 3
    assert "UNIT OWNERS' ASSOCIATION" in statutory.get(OLD).text
    assert "UNIT OWNERS ASSOCIATION" in statutory.get(NEW).text


def test_unit_g_timeline(assoc):
    # First notice by day 30 (board left late_notice unset: drafted on day 1).
    assert delinquency.run(assoc, DUE).actions == []
    delinquency.run(assoc, day(1))
    assert steps() == ["first_notice"]
    case = DelinquencyCase.objects.get(unit__number="G")
    cfg = delinquency.statutory_config(assoc, day(1))
    assert delinquency.first_notice_deadline(case, cfg) == day(30)
    assert delinquency.standstill_until(case, cfg) == day(16)
    # Standstill: a board demand step at day 10 waits until it ends.
    delinquency.set_offset(assoc, "demand", 10, reason="test")
    run_days(assoc, 2, 16)
    assert steps() == ["first_notice"]
    delinquency.run(assoc, day(17))
    assert steps() == ["first_notice", "demand"]
    # Second notice: not before day 90 past due and 60 days after the first.
    run_days(assoc, 18, 89)
    assert steps() == ["first_notice", "demand"]
    delinquency.run(assoc, day(90))
    assert steps() == ["first_notice", "demand", "second_notice"]
    assert action("second_notice").statutory_text == OLD


def test_second_notice_waits_60_days_after_a_late_first(assoc):
    delinquency.set_offset(assoc, "late_notice", 45, reason="test")  # capped at 30
    delinquency.run(assoc, day(29))
    assert steps() == []
    case = DelinquencyCase.objects.get(unit__number="G")
    delinquency.set_hold(case, True, reason="talking to the owner")
    delinquency.run(assoc, day(30))
    delinquency.set_hold(case, False, reason="no agreement")
    delinquency.run(assoc, day(40))  # late: the deadline was day 30
    assert steps() == ["first_notice"]
    delinquency.run(assoc, day(90))
    delinquency.run(assoc, day(99))
    assert steps() == ["first_notice"]  # 90 days past due, but not 60 after the first
    delinquency.run(assoc, day(100))
    assert steps() == ["first_notice", "second_notice"]


def test_mail_to_every_address_and_email_without_consent(assoc):
    delinquency.run(assoc, day(1))
    first = action("first_notice")
    records = list(first.records.all())
    mail = [r for r in records if r.channel == "mail"]
    email = [r for r in records if r.channel == "email"]
    assert sorted(r.address_lines[1] for r in mail) == ["1 Cedar Row G", "9 Elsewhere Ave"]
    assert {x.letter_id for x in mail} <= set(first.batch.items.values_list("letter", flat=True))
    assert first.batch.status == MailBatch.Status.DRAFT
    # No e-delivery consent was recorded, yet the statute's email copy is made (preview).
    assert [r.email for r in email] == ["g@ex.org"]
    note = email[0].notification
    assert note.type_key == delinquency.NOTICE_NOTIFICATION
    assert "THIS IS A NOTICE OF DELINQUENCY" in note.body
    assert list(note.deliveries.values_list("channel", "status")) == [
        ("email", Delivery.Status.PREVIEW)
    ]
    assert not Delivery.objects.exclude(status=Delivery.Status.PREVIEW).exists()


def test_notice_pdf_has_the_locked_block_and_marks_missing_contacts(assoc):
    delinquency.run(assoc, day(1))
    first = action("first_notice")
    assert first.contacts_missing
    text = statutory.render(OLD, assoc)[0]
    assert statutory.placeholder() in text
    assert letter_bytes(first.letter).startswith(b"%PDF")
    ids = {i.id for i in todos.collect(assoc)}
    assert "delinquency-contacts" in ids

    full = {"phone": "1-800-000-0000", "url": "https://example.org"}
    delinquency.set_contacts(
        assoc,
        {slot: full for slot, _label in statutory.CONTACT_SLOTS},
        reason="numbers from Commerce",
    )
    text, missing = statutory.render(OLD, assoc)
    assert not missing
    assert text.count("Telephone: 1-800-000-0000 Website: https://example.org") == 3
    # Every non-contact line of the statute is printed unchanged.
    for line in statutory.get(OLD).text.splitlines():
        if not statutory.CONTACT_LINE.match(line):
            assert line in text.splitlines()
    assert "delinquency-contacts" not in {i.id for i in todos.collect(assoc)}
    assert AuditEvent.objects.filter(action="delinquency.contacts").exists()


def test_2028_variant_picked_by_notice_date(assoc):
    assert delinquency.statutory_config(assoc, dt.date(2027, 12, 31))["text"] == OLD
    assert delinquency.statutory_config(assoc, dt.date(2028, 1, 1))["text"] == NEW
    case = DelinquencyCase.objects.create(
        association=assoc,
        unit=Unit.objects.get(number="G"),
        anchor_due_date=DUE,
        opened_on=DUE,
    )
    cfg = delinquency.statutory_config(assoc, dt.date(2028, 1, 2))
    a, _letters = delinquency.take_notice(case, "first_notice", dt.date(2028, 1, 2), cfg)
    assert a.statutory_text == NEW


def test_first_notice_todo_shows_deadline(assoc, settings):
    settings.HOMEOA_FIXED_TODAY = day(31).isoformat()
    delinquency.set_hold(
        DelinquencyCase.objects.get_or_create(
            association=assoc,
            unit=Unit.objects.get(number="G"),
            defaults={"anchor_due_date": DUE, "opened_on": DUE},
        )[0],
        True,
        reason="test",
    )
    delinquency.run(assoc, day(31))
    item = next(i for i in todos.collect(assoc) if i.id.startswith("delinquency-first-notice"))
    assert item.due == day(30)
    assert todos.group_of(item, day(31)) == "today"  # overdue groups with today


def test_threshold_todo_excludes_fines(assoc, settings):
    unit = Unit.objects.get(number="G")
    run_days(assoc, 1, 1)
    delinquency.run(assoc, day(90))
    assert "second_notice" in steps()
    # 2026-09..2027-02 = 6 x $350 = $2,100 >= max(3 x 350, 2000) once Feb 10 is past due.
    feb = dt.date(2027, 2, 11)
    case = DelinquencyCase.objects.get(unit=unit)
    assert delinquency.owed_assessments(case, feb) == D("2100.00")
    # A late fee is not an assessment: reclassify one month and the threshold is missed.
    oct_charge = Charge.objects.get(unit=unit, period=dt.date(2026, 10, 1))
    Charge.objects.filter(pk=oct_charge.pk).update(kind="late_fee")
    delinquency.run(assoc, feb)
    case.refresh_from_db()
    assert case.threshold_reached_on is None
    Charge.objects.filter(pk=oct_charge.pk).update(kind="assessment")
    delinquency.run(assoc, feb)
    case.refresh_from_db()
    assert case.threshold_reached_on == feb

    settings.HOMEOA_FIXED_TODAY = (feb + dt.timedelta(days=89)).isoformat()
    assert not [i for i in todos.collect(assoc) if "foreclosure" in i.id]
    settings.HOMEOA_FIXED_TODAY = (feb + dt.timedelta(days=90)).isoformat()
    items = [i for i in todos.collect(assoc) if "foreclosure" in i.id]
    assert len(items) == 1
    assert "foreclosure threshold" in items[0].title and "attorney" in items[0].title
    assert AuditEvent.objects.filter(action="delinquency.threshold").exists()


def test_preforeclosure_records_are_protected(assoc):
    delinquency.run(assoc, day(1))
    record = PreforeclosureRecord.objects.first()
    with pytest.raises(ProtectedRecordError):
        record.delete()
    with pytest.raises(ProtectedError):
        action("first_notice").delete()
    with pytest.raises(ProtectedError):
        Letter.objects.filter(pk=record.letter_id).delete()
    # Re-running never replaces a notice.
    letters = Letter.objects.count()
    delinquency.run(assoc, day(2))
    delinquency.run(assoc, day(1))
    assert Letter.objects.count() == letters
    assert PreforeclosureRecord.objects.count() == 3


def test_without_the_pack_keys_the_old_ladder_runs(db, settings, tmp_path):
    a = Association.objects.create(name="X", short_name="X")
    assert delinquency.statutory_config(a, DUE) is None
    assert delinquency.statutory_todo_items(a) == []


# --- views ------------------------------------------------------------------------------


@pytest.fixture
def board_client(client, assoc):
    user = get_user_model().objects.create_user(email="treasurer@example.org")
    grant(user, association=assoc, title="treasurer", start=dt.date(2026, 1, 1))
    client.force_login(user)
    return client


def test_pages_show_deadline_standstill_records_and_locked_text(board_client, assoc, settings):
    settings.HOMEOA_FIXED_TODAY = day(5).isoformat()
    delinquency.run(assoc, day(1))
    page = board_client.get("/money/delinquency/").content.decode()
    assert 'data-testid="statutory-mode"' in page
    assert "First notice due by" in page
    assert "Standstill until" in page
    assert 'data-testid="preforeclosure-records"' in page
    page = board_client.get("/settings/delinquency/").content.decode()
    assert "THIS IS A NOTICE OF DELINQUENCY FOR PAST DUE ASSESSMENTS" in page
    assert 'data-testid="contacts-missing"' in page
    data = {"contacts": "1", "reason": "from Commerce"}
    for slot, _label in statutory.CONTACT_SLOTS:
        data[f"{slot}_phone"] = "1-800-000-0000"
        data[f"{slot}_url"] = "https://example.org"
    assert board_client.post("/settings/delinquency/", data).status_code == 302
    assert not statutory.contacts_missing(Association.objects.get(pk=assoc.pk))
    assert Notification.objects.exists()
