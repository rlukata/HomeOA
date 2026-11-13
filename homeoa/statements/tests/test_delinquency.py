import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa import rules
from homeoa.access.testing import grant, make_live, open_residents
from homeoa.audit.models import AuditEvent
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import default_schedule_kwargs, generate_assessments
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import todos
from homeoa.jobs import periodic_jobs
from homeoa.letters.models import Letter, MailBatch
from homeoa.notify.models import Delivery, Notification
from homeoa.registry.models import NoticePreference, Person, Unit
from homeoa.registry.services import set_initial_ownership
from homeoa.statements import delinquency
from homeoa.statements.models import DelinquencyAction, DelinquencyCase

D = Decimal
SEP = dt.date(2026, 9, 1)
DUE = dt.date(2026, 9, 10)
# Test offsets only; real values come from the rule pack / board (none are in core).
OFFSETS = {"reminder": 15, "late_notice": 30, "demand": 60}


def day(n: int) -> dt.date:
    return DUE + dt.timedelta(days=n)


@pytest.fixture
def assoc(db, settings, tmp_path):
    from homeoa.ledger.chart import load_chart_of_accounts

    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        for number in ("P", "Q", "R"):
            unit = Unit.objects.create(
                association=a,
                number=number,
                allocated_interest=D("33.3333") if number != "R" else D("33.3334"),
                address_line1=f"1 Cedar Row {number}",
                city="Tukwila",
                region="WA",
                postal_code="98188",
            )
            person = Person.objects.create(
                association=a, full_name=f"Owner {number}", email=f"{number.lower()}@ex.org"
            )
            set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
        s = AssessmentSchedule.objects.create(
            association=a,
            name="Dues",
            annual_amount=D("12000.00"),
            effective_start=dt.date(2026, 1, 1),
            **{**default_schedule_kwargs(a), "due_day": 10},
        )
        generate_assessments(s, SEP)
        from homeoa.payments.services import record_receipt

        unit_p = Unit.objects.get(number="P")
        record_receipt(
            unit=unit_p,
            amount=Charge.objects.get(unit=unit_p).amount,
            received_date=SEP,
            method="check",
        )
        for step, days in OFFSETS.items():
            rules.set_override(a, f"delinquency.{step}_days", days, reason="test offsets")
        yield a


def consent(unit_number):
    person = Person.objects.get(full_name=f"Owner {unit_number}")
    NoticePreference.objects.create(
        person=person,
        delivery="email",
        e_delivery_consent=True,
        email=person.email,
        source="portal",
    )


def case_for(number):
    return DelinquencyCase.objects.get(unit__number=number, status="open")


def steps(number):
    return list(case_for(number).actions.values_list("step", flat=True))


def test_offsets_come_from_rules_unset_by_default(db):
    a = Association.objects.create(name="X", short_name="X")
    offsets = rules.delinquency_offsets(a)
    assert list(offsets) == ["reminder", "late_notice", "demand"]
    assert all(r.value is None and not r.verified for r in offsets.values())


def test_nothing_happens_without_offsets(db, settings, tmp_path, assoc):
    for step in OFFSETS:
        rules.clear_override(assoc, f"delinquency.{step}_days", reason="off")
    result = delinquency.run(assoc, day(90))
    assert not result.actions
    assert Letter.objects.count() == 0
    assert {c.unit.number for c in result.opened} == {"Q", "R"}


def test_ladder_progresses_at_configured_offsets(assoc):
    consent("Q")
    assert delinquency.run(assoc, day(14)).actions == []
    assert steps("Q") == []
    r = delinquency.run(assoc, day(15))
    assert {a.case.unit.number for a in r.actions} == {"Q", "R"}
    assert steps("Q") == ["reminder"]
    assert delinquency.run(assoc, day(15)).actions == []  # idempotent
    assert delinquency.run(assoc, day(29)).actions == []
    delinquency.run(assoc, day(30))
    assert steps("Q") == ["reminder", "late_notice"]
    delinquency.run(assoc, day(59))
    assert steps("Q") == ["reminder", "late_notice"]
    delinquency.run(assoc, day(60))
    assert steps("Q") == ["reminder", "late_notice", "demand"]
    assert case_for("Q").anchor_due_date == DUE
    assert "P" not in {c.unit.number for c in DelinquencyCase.objects.all()}

    # Q consented to email: notify messages, filed on the unit, previews (residents off).
    notes = Notification.objects.filter(unit__number="Q")
    assert {n.type_key for n in notes} == set(delinquency.NOTIFICATION_TYPES.values())
    assert set(
        Delivery.objects.filter(notification__in=notes).values_list("status", flat=True)
    ) == {Delivery.Status.PREVIEW}
    # R has no consent: its letters go into draft postal batches.
    r_actions = DelinquencyAction.objects.filter(case__unit__number="R").exclude(step="demand")
    assert all(a.batch is not None for a in r_actions)
    assert set(MailBatch.objects.values_list("status", flat=True)) == {MailBatch.Status.DRAFT}
    assert not Delivery.objects.exclude(status=Delivery.Status.PREVIEW).exists()

    # Demand: a draft letter and a board to-do, never routed.
    demand = DelinquencyAction.objects.get(case__unit__number="Q", step="demand")
    assert demand.letter is not None and demand.batch is None
    assert not Notification.objects.filter(object_key=f"letters.letter:{demand.letter_id}")
    items = [i for i in todos.collect(assoc) if i.id.startswith("delinquency-demand")]
    assert len(items) == 2
    delinquency.mark_reviewed(demand)
    items = [i for i in todos.collect(assoc) if i.id.startswith("delinquency-demand")]
    assert len(items) == 1


def test_arrears_from_before_go_live_start_the_ladder_fresh_at_go_live(assoc):
    go_live = day(120)
    make_live(assoc, since=go_live)
    # Each daily run from go-live on takes a step only once its offset from go-live passes.
    for n in range(0, 61):
        delinquency.run(assoc, go_live + dt.timedelta(days=n))
        taken = steps("Q")
        expected = [s for s, off in OFFSETS.items() if n >= off]
        assert taken == expected, (n, taken)
    case = case_for("Q")
    assert case.anchor_due_date == go_live
    # The real oldest due date is still what the board sees.
    assert delinquency.oldest_due(case, go_live) == DUE


def test_without_a_go_live_date_the_ladder_counts_from_the_oldest_due_date(assoc):
    delinquency.run(assoc, day(120))
    assert case_for("Q").anchor_due_date == DUE
    assert steps("Q") == ["reminder"]


def test_arrears_due_after_go_live_anchor_on_their_due_date(assoc):
    make_live(assoc, since=SEP)
    delinquency.run(assoc, day(15))
    assert case_for("Q").anchor_due_date == DUE
    assert steps("Q") == ["reminder"]


def test_delinquency_page_shows_the_real_oldest_due_date(board_client, assoc):
    make_live(assoc, since=day(120))
    delinquency.run(assoc, day(120))
    page = board_client.get("/money/delinquency/").content.decode()
    assert "Sep 10, 2026" in page


def test_payment_in_full_stops_the_ladder(assoc):
    from homeoa.payments.services import record_receipt

    delinquency.run(assoc, day(15))
    q = Unit.objects.get(number="Q")
    record_receipt(
        unit=q, amount=Charge.objects.get(unit=q).amount, received_date=day(20), method="check"
    )
    r = delinquency.run(assoc, day(30))
    assert [c.unit.number for c in r.closed] == ["Q"]
    case = DelinquencyCase.objects.get(unit=q)
    assert case.status == "closed"
    assert list(case.actions.values_list("step", flat=True)) == ["reminder"]
    assert AuditEvent.objects.filter(action="delinquency.close").exists()


def test_hold_and_skip_are_audited(assoc):
    delinquency.run(assoc, day(15))
    q = case_for("Q")
    with pytest.raises(delinquency.DelinquencyError):
        delinquency.set_hold(q, True, reason="")
    delinquency.set_hold(q, True, reason="payment plan agreed")
    delinquency.run(assoc, day(30))
    assert steps("Q") == ["reminder"]
    delinquency.set_hold(q, False, reason="plan broken")
    delinquency.skip_next(q, day(31), reason="go straight to demand")
    delinquency.run(assoc, day(31))
    assert steps("Q") == ["reminder", "late_notice"]
    assert case_for("Q").actions.get(step="late_notice").outcome == "skipped"
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"delinquency.hold", "delinquency.release", "delinquency.skip"} <= actions


def test_override_is_audited_with_reason(assoc):
    delinquency.set_offset(assoc, "reminder", 20, reason="board vote")
    assert rules.delinquency_offsets(assoc)["reminder"].value == 20
    event = AuditEvent.objects.filter(action="rules.override").order_by("-pk").first()
    assert event.changes["reason"] == "board vote"
    delinquency.set_offset(assoc, "reminder", None, reason="back to pack")
    assert rules.delinquency_offsets(assoc)["reminder"].value is None


def test_job_registered():
    assert "statements.delinquency_ladder" in periodic_jobs()


# --- views ------------------------------------------------------------------------------


@pytest.fixture
def board_client(client, assoc):
    user = get_user_model().objects.create_user(email="treasurer@example.org")
    grant(user, association=assoc, title="treasurer", start=dt.date(2026, 1, 1))
    client.force_login(user)
    return client


def test_pages(board_client, assoc, settings):
    settings.HOMEOA_FIXED_TODAY = day(15).isoformat()
    resp = board_client.get("/settings/delinquency/")
    assert resp.status_code == 200
    assert b"Unverified" in resp.content
    resp = board_client.post("/money/delinquency/run/")
    assert resp.status_code == 302
    resp = board_client.get("/money/delinquency/")
    assert resp.status_code == 200
    assert b'data-unit="Q"' in resp.content
    case = case_for("Q")
    resp = board_client.post(f"/money/delinquency/{case.pk}/hold/", {"reason": "", "on_hold": "1"})
    case.refresh_from_db()
    assert not case.on_hold
    board_client.post(f"/money/delinquency/{case.pk}/hold/", {"reason": "plan", "on_hold": "1"})
    case.refresh_from_db()
    assert case.on_hold


def test_settings_post(board_client, assoc):
    data = {"reminder": "21", "late_notice": "30", "demand": "60", "reason": ""}
    resp = board_client.post("/settings/delinquency/", data)
    assert resp.status_code == 400
    data["reason"] = "board vote"
    resp = board_client.post("/settings/delinquency/", data)
    assert resp.status_code == 302
    assoc.refresh_from_db()
    assert rules.delinquency_offsets(assoc)["reminder"].value == 21


def test_pages_need_permission(client, assoc):
    user = get_user_model().objects.create_user(email="owner@example.org")
    client.force_login(user)
    assert client.get("/money/delinquency/").status_code in (302, 403)


@pytest.mark.parametrize("residents_on", [False, True])
def test_board_member_owner_only_gets_previews(assoc, residents_on):
    from django.core import mail

    from homeoa.jobs.testing import capture_jobs

    user = get_user_model().objects.create_user(email="q@ex.org")
    grant(user, association=assoc, start=dt.date(2026, 1, 1))
    person = Person.objects.get(full_name="Owner Q")
    person.user = user
    person.save()
    consent("Q")
    if residents_on:
        open_residents(assoc)
    with capture_jobs() as queue:
        delinquency.run(assoc, day(15))
        assert queue.pending() == []
    notes = Notification.objects.filter(unit__number="Q")
    assert notes.exists()
    statuses = set(Delivery.objects.filter(notification__in=notes).values_list("status", flat=True))
    assert statuses == {Delivery.Status.PREVIEW}
    assert mail.outbox == []


class _Req:
    def __init__(self, user, association):
        self.user, self.association = user, association


def test_money_nav_has_delinquency_for_board_only(client, assoc):
    from homeoa.ui.nav import get_hub, hidden_subtab_urls

    board = get_user_model().objects.create_user(email="t@example.org")
    grant(board, association=assoc, title="treasurer", start=dt.date(2026, 1, 1))
    other = get_user_model().objects.create_user(email="nobody@example.org")
    assert "delinquency" in [t.slug for t in get_hub("money").subtabs]
    assert "/money/delinquency/" not in hidden_subtab_urls(_Req(board, assoc))
    assert "/money/delinquency/" in hidden_subtab_urls(_Req(other, assoc))
    client.force_login(board)
    assert b'href="/money/delinquency/"' in client.get("/money/assessments/").content
