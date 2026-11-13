"""Resale certificates: the seller asks or the board logs a request, HomeOA pre-fills what
it knows, the board answers the rest and issues; the fee posts to the seller's account."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from homeoa.access.testing import open_residents
from homeoa.billing.models import Charge
from homeoa.changes.models import ChangeRequest
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.dashboard import todos
from homeoa.notify.models import Notification
from homeoa.plugins import registry as plugin_registry
from homeoa.registry.models import Unit
from homeoa.resale import services
from homeoa.resale.models import ResaleRequest

User = get_user_model()
TREASURER = "treasurer@cedarrow.example"
SECRETARY = "secretary@cedarrow.example"
OWNER_C = "ownerc@cedarrow.example"
OWNER_A = "ownera@cedarrow.example"
TENANT = "chris.doyle@cedarrow.example"
TODAY = dt.date(2026, 10, 7)


@pytest.fixture
def pilot(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    settings.HOMEOA_FIXED_TODAY_ALLOWED = True
    settings.HOMEOA_FIXED_TODAY = TODAY.isoformat()
    plugin_registry.load(settings.HOMEOA_PLUGINS)
    a = demo.seed()
    with association_context(a):
        open_residents(a)
        yield a


def _ask(client, django_capture_on_commit_callbacks):
    client.force_login(User.objects.get(email=OWNER_C))
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(reverse("resale_portal:index"), {"send_to": "escrow@title.example"})
    assert resp.status_code == 302
    return ResaleRequest.objects.get()


def _answer_all(req):
    services.save_answers(req, {i["key"]: i["answer"] or "None." for i in services.items(req)})


def test_seller_asks_and_homeoa_prefills(pilot, client, django_capture_on_commit_callbacks):
    req = _ask(client, django_capture_on_commit_callbacks)
    assert req.unit.number == "C" and req.form_id == "wa_64_34_425"
    assert req.due_on == TODAY + dt.timedelta(days=10)
    assert "Monthly assessment" in req.answers["b"]
    assert "Evergreen Mutual" in req.answers["l"]
    assert "Bylaws" in req.answers["q"]
    assert req.answers["k"] == ""  # only the board can answer pending suits
    assert Notification.objects.filter(type_key=services.REQUESTED_TYPE).exists()
    assert f"resale-{req.pk}" in [t.id for t in todos.collect(pilot)]


def test_issue_needs_every_answer_then_charges_the_seller(
    pilot, client, django_capture_on_commit_callbacks
):
    req = _ask(client, django_capture_on_commit_callbacks)
    client.force_login(User.objects.get(email=TREASURER))
    client.post(reverse("resale:issue", args=[req.pk]), {"fee": "275.00"})
    req.refresh_from_db()
    assert req.status == ResaleRequest.Status.OPEN  # pending suits etc. unanswered
    _answer_all(req)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("resale:issue", args=[req.pk]), {"fee": "275.00"})
    req.refresh_from_db()
    assert req.status == ResaleRequest.Status.ISSUED and req.pdf_key
    charge = Charge.objects.get(pk=req.charge_id)
    assert charge.amount == Decimal("275.00") and charge.unit == req.unit
    note = Notification.objects.get(
        type_key=services.CERTIFICATE_TYPE, recipient__email="escrow@title.example"
    )
    assert note.attachments and note.attachments[0]["file_key"] == req.pdf_key
    assert client.get(reverse("resale:pdf", args=[req.pk])).content.startswith(b"%PDF")
    assert f"resale-{req.pk}" not in [t.id for t in todos.collect(pilot)]


def test_fee_above_the_cap_is_refused(pilot):
    req = services.open_request(
        pilot,
        unit=Unit.objects.get(number="A"),
        on=TODAY,
        send_to="agent@x.example",
        requester_name="Agent",
    )
    _answer_all(req)
    with pytest.raises(services.ResaleError):
        services.issue(req, fee=Decimal("300.00"), user=None, on=TODAY)


def test_other_board_members_send_issuing_for_approval(pilot, client):
    req = services.open_request(
        pilot,
        unit=Unit.objects.get(number="A"),
        on=TODAY,
        send_to="agent@x.example",
        requester_name="Agent",
    )
    _answer_all(req)
    client.force_login(User.objects.get(email=SECRETARY))
    client.post(reverse("resale:issue", args=[req.pk]), {"fee": "100.00"})
    req.refresh_from_db()
    assert req.status == ResaleRequest.Status.OPEN
    assert ChangeRequest.objects.filter(kind="resale.issue").exists()


def test_seller_downloads_own_certificate_only(pilot, client, django_capture_on_commit_callbacks):
    req = _ask(client, django_capture_on_commit_callbacks)
    _answer_all(req)
    services.issue(req, fee=Decimal("0"), user=None, on=TODAY)
    assert client.get(reverse("resale_portal:pdf", args=[req.pk])).status_code == 200
    client.force_login(User.objects.get(email=OWNER_A))
    assert client.get(reverse("resale_portal:pdf", args=[req.pk])).status_code == 404


def test_tenants_have_no_resale_page(pilot, client):
    client.force_login(User.objects.get(email=TENANT))
    assert "more-resale" not in client.get(reverse("portal:more")).content.decode()
    assert client.get(reverse("resale_portal:index")).status_code == 403


def test_statement_of_unpaid_assessments_for_a_lender(pilot):
    """RCW 64.34.364(15): a signed statement within 15 days; the fee is only "reasonable"."""
    req = services.open_request(
        pilot,
        unit=Unit.objects.get(number="C"),
        on=TODAY,
        send_to="loans@bank.example",
        requester_name="Evergreen Bank",
        kind=ResaleRequest.Kind.STATEMENT,
    )
    assert req.form_id == "unpaid_statement" and req.due_on == TODAY + dt.timedelta(days=15)
    assert req.answers["1"] and req.answers["2"]
    assert str(req).startswith("Statement of unpaid assessments")
    assert services.fee_cap(pilot, TODAY, req.kind) is None
    services.issue(req, fee=Decimal("300.00"), user=User.objects.get(email=TREASURER), on=TODAY)
    req.refresh_from_db()
    assert req.status == ResaleRequest.Status.ISSUED
    assert Charge.objects.get(pk=req.charge_id).description.startswith("Statement of unpaid")


def test_2028_requests_use_the_rcw_64_90_640_form(pilot):
    req = services.open_request(
        pilot,
        unit=Unit.objects.get(number="A"),
        on=dt.date(2028, 1, 3),
        send_to="agent@x.example",
        requester_name="Agent",
    )
    assert req.form_id == "wa_64_90_640"
    assert req.answers["z"].startswith("THIS UNIT IS LOCATED WITHIN A COMMON INTEREST COMMUNITY")
    assert req.answers["y"].startswith("This association does not have a current reserve study.")


def test_violations_answer_comes_from_the_violations_module(pilot):
    from types import SimpleNamespace

    from homeoa.enforcement.models import Violation, ViolationCategory
    from homeoa.plugins import slots
    from homeoa.resale import prefill

    found = [
        c
        for c in slots.contributions(slots.RESALE_CERTIFICATE_ANSWERS)
        if c.value[0] == "violations"
    ]
    assert [c.module for c in found] == ["violations"]
    assert "violations" not in prefill.SOURCES

    unit = Unit.objects.get(number="C")
    Violation.objects.filter(unit=unit).delete()
    item = SimpleNamespace(source="violations")
    assert prefill._answer(item, unit, TODAY) == "None known."
    cat = ViolationCategory.objects.create(name="Noise", rule_citation="Rules 4.2")
    Violation.objects.create(
        unit=unit, category=cat, observed_on=dt.date(2026, 8, 15), description="Loud music"
    )
    assert prefill._answer(item, unit, TODAY) == "Open: Noise, observed 2026-08-15."


def _violations_off(association):
    from homeoa.enforcement.models import Violation
    from homeoa.plugins import modules

    Violation.objects.all().delete()  # a module with open items cannot be turned off
    modules.set_module(association, "violations", False, user=None)


@pytest.mark.parametrize(
    ("on", "form_id", "key"),
    [(dt.date(2026, 10, 7), "wa_64_34_425", "m"), (dt.date(2028, 1, 3), "wa_64_90_640", "m")],
)
def test_with_violations_off_the_violations_item_says_the_board_must_answer(
    pilot, on, form_id, key
):
    _violations_off(pilot)
    req = services.open_request(
        pilot, unit=Unit.objects.get(number="A"), on=on, send_to="a@x.example", requester_name="A"
    )
    assert req.form_id == form_id
    assert req.answers[key] == (
        "The association does not track violations in HomeOA. The board must answer this item."
    )
    assert "None known." not in req.answers.values()


def test_with_violations_on_both_forms_still_answer_from_the_violations_records(pilot):
    from homeoa.enforcement.models import Violation

    Violation.objects.all().delete()
    for on in (dt.date(2026, 10, 7), dt.date(2028, 1, 3)):
        req = services.open_request(
            pilot,
            unit=Unit.objects.get(number="A"),
            on=on,
            send_to="a@x.example",
            requester_name="A",
        )
        assert req.answers["m"] == "None known."


def test_an_off_module_without_its_own_note_gets_the_generic_one(pilot, monkeypatch):
    from types import SimpleNamespace

    from homeoa.plugins import slots
    from homeoa.resale import prefill

    _violations_off(pilot)
    monkeypatch.setattr(
        slots,
        "_contributions",
        {
            **slots._contributions,
            slots.RESALE_CERTIFICATE_ANSWERS: [
                slots.Contribution(
                    slots.RESALE_CERTIFICATE_ANSWERS,
                    ("violations", lambda unit, on: "None known."),
                    module="violations",
                )
            ],
        },
    )
    item = SimpleNamespace(source="violations")
    answer = prefill._answer(item, Unit.objects.get(number="A"), TODAY)
    assert answer == "Not tracked in HomeOA. The board must answer this item."


def test_issuing_waits_until_the_board_answers_an_untracked_item(pilot, client):
    _violations_off(pilot)
    req = services.open_request(
        pilot,
        unit=Unit.objects.get(number="A"),
        on=TODAY,
        send_to="a@x.example",
        requester_name="A",
    )
    answers = {i["key"]: i["answer"] or "None." for i in services.items(req)}
    services.save_answers(req, answers)
    client.force_login(User.objects.get(email=TREASURER))
    page = client.get(reverse("resale:detail", args=[req.pk])).content.decode()
    assert "Needs your answer" in page
    with pytest.raises(services.ResaleError, match="does not track"):
        services.issue(req, fee=Decimal("0.00"), user=None, on=TODAY)
    req.refresh_from_db()
    assert req.status == ResaleRequest.Status.OPEN

    services.save_answers(req, {**answers, "m": "None known to the board."})
    assert not any(i["needs_answer"] for i in services.items(req))
    assert (
        "Needs your answer"
        not in client.get(reverse("resale:detail", args=[req.pk])).content.decode()
    )
    services.issue(req, fee=Decimal("0.00"), user=User.objects.get(email=TREASURER), on=TODAY)
    req.refresh_from_db()
    assert req.status == ResaleRequest.Status.ISSUED
