from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.access.testing import grant
from homeoa.billing.models import Adjustment, Charge
from homeoa.billing.services import generate_assessments
from homeoa.payments.models import Receipt, ReceiptStatus

from .test_services import OCT, assoc, schedule, unit  # noqa: F401

D = Decimal


def _login(client, association, email, **kw):
    user = get_user_model().objects.create_user(email=email)
    grant(user, association=association, **kw)
    client.force_login(user)
    return user


@pytest.fixture
def treasurer(assoc, client):  # noqa: F811
    _login(client, assoc, "t@example.com", title="treasurer")
    return client


@pytest.fixture
def member(assoc, client):  # noqa: F811
    _login(client, assoc, "m@example.com")
    return client


def _post_payment(c, n, amount):
    return c.post(
        "/money/payments/new/",
        {
            "unit": unit(n).pk,
            "amount": amount,
            "received_date": "2026-10-05",
            "method": "check",
            "reference": "1042",
            "deposit_account": _cash(),
        },
    )


def _cash():
    from homeoa.ledger.models import Account

    return Account.objects.get(code="1000").pk


def test_record_payment_flow(treasurer, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    assert treasurer.get("/money/payments/new/").status_code == 200
    resp = _post_payment(treasurer, 7, "377.52")
    assert resp.status_code == 302
    r = Receipt.objects.get()
    page = treasurer.get(resp["Location"])
    html = page.content.decode()
    assert 'data-testid="allocation"' in html
    assert 'data-testid="unit-balance"' in html
    assert "$0.00" in html
    assert r.created_by is not None


def test_htmx_post_returns_result_partial(treasurer, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    resp = treasurer.post(
        "/money/payments/new/",
        {
            "unit": unit(8).pk,
            "amount": "385.40",
            "received_date": "2026-10-05",
            "method": "check",
            "deposit_account": _cash(),
        },
        HTTP_HX_REQUEST="true",
    )
    html = resp.content.decode()
    assert resp.status_code == 200
    assert 'data-testid="allocation"' in html
    assert "-$50.00" in html


def test_unit_summary_partial(treasurer, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    resp = treasurer.get(f"/money/payments/unit-summary/?unit={unit(7).pk}")
    assert "$377.52" in resp.content.decode()


def test_void_and_refund_views(treasurer, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    _post_payment(treasurer, 8, "385.40")
    r = Receipt.objects.get()
    treasurer.post(
        f"/money/payments/{r.pk}/refund/",
        {"amount": "50.00", "date": "2026-10-09", "method": "check", "reason": "asked"},
    )
    assert r.refunds.count() == 1
    _post_payment(treasurer, 7, "10.00")
    r2 = Receipt.objects.get(unit=unit(7))
    treasurer.post(f"/money/payments/{r2.pk}/void/", {"reason": "Bounced"})
    r2.refresh_from_db()
    assert r2.status == ReceiptStatus.VOID


def test_adjustment_requires_reason(treasurer, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    data = {"kind": "write_off", "unit": unit(7).pk, "amount": "10.00", "date": "2026-10-09"}
    resp = treasurer.post("/money/adjustments/", data)
    assert resp.status_code == 400
    assert not Adjustment.objects.exists()
    resp = treasurer.post("/money/adjustments/", {**data, "reason": "Uncollectible"})
    assert resp.status_code == 302
    assert Adjustment.objects.get().account.code == "5800"


def test_void_charge_via_adjustments(treasurer, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    c = Charge.objects.get(unit=unit(7))
    resp = treasurer.post(
        "/money/adjustments/",
        {"kind": "charge_void", "unit": unit(7).pk, "charge": c.pk, "date": "2026-10-09",
         "reason": "Billed in error"},
    )  # fmt: skip
    assert resp.status_code == 302
    c.refresh_from_db()
    assert c.void_entry_id


def test_board_member_proposes_money_changes(assoc, client, schedule):  # noqa: F811
    """A board member who is not the Treasurer sees the money screens and proposes; the
    change waits for another board member, then posts as the proposer."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    treasurer = get_user_model().objects.create_user(email="t@example.com")
    grant(treasurer, association=assoc, title="treasurer")
    member = _login(client, assoc, "m@example.com")
    generate_assessments(schedule, OCT)
    assert client.get("/money/payments/new/").status_code == 200
    resp = _post_payment(client, 7, "377.52")
    change = ChangeRequest.objects.get()
    assert resp.status_code == 302 and resp["Location"] == f"/governance/changes/{change.pk}/"
    assert change.summary == "Record a $377.52 payment from Unit 7"
    assert change.required_approvals == 1 and not Receipt.objects.exists()
    changes.approve(change, treasurer)
    change.refresh_from_db()
    assert change.status == ChangeRequest.Status.APPLIED, change.error
    receipt = Receipt.objects.get()
    assert receipt.amount == D("377.52") and receipt.entry.posted_by == member
    assert change.result_url == f"/money/payments/{receipt.pk}/"


def test_board_member_proposal_shows_form_errors(assoc, client, schedule):  # noqa: F811
    from homeoa.changes.models import ChangeRequest

    grant(get_user_model().objects.create_user(email="t@example.com"), association=assoc)
    _login(client, assoc, "m@example.com")
    resp = client.post("/money/payments/new/", {"unit": unit(7).pk, "amount": "-5"})
    assert resp.status_code == 400
    assert not ChangeRequest.objects.exists() and not Receipt.objects.exists()


def test_credits_always_need_a_second_board_member(assoc, client, schedule):  # noqa: F811
    """Even the Treasurer's credits, write-offs and voided charges are change requests."""
    from homeoa.changes import services as changes
    from homeoa.changes.models import ChangeRequest

    other = get_user_model().objects.create_user(email="p@example.com")
    grant(other, association=assoc, title="president")
    _login(client, assoc, "t@example.com", title="treasurer")
    generate_assessments(schedule, OCT)
    c = Charge.objects.get(unit=unit(7))
    data = {"kind": "credit_memo", "unit": unit(7).pk, "charge": c.pk, "amount": "10.00",
            "date": "2026-10-09", "reason": "Goodwill"}  # fmt: skip
    client.post("/money/adjustments/", data)
    change = ChangeRequest.objects.get()
    assert change.summary == "Credit memo of $10.00 for Unit 7"
    assert not Adjustment.objects.exists()
    changes.approve(change, other)
    assert Adjustment.objects.get().amount == D("10.00")


def test_owner_cannot_propose_money_changes(assoc, client, schedule):  # noqa: F811
    from homeoa.changes.models import ChangeRequest

    client.force_login(get_user_model().objects.create_user(email="o@example.com"))
    assert client.get("/money/payments/new/").status_code == 403
    assert _post_payment(client, 7, "1.00").status_code == 403
    assert not ChangeRequest.objects.exists()


def test_anonymous_redirects(client, assoc):  # noqa: F811
    assert client.get("/money/payments/new/").status_code in (302, 403)


def test_ledger_home(treasurer, schedule):  # noqa: F811
    from homeoa.ledger.services import unit_balance

    generate_assessments(schedule, OCT)
    _post_payment(treasurer, 7, "100.00")
    resp = treasurer.get("/money/ledger/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'data-testid="ledger-home"' in html
    assert 'class="on" aria-current="page">Dues &amp; ledger</a>' in html
    rows = {r["unit"].number: r for r in resp.context["rows"]}
    assert len(rows) == 11
    for number, r in rows.items():
        assert r["balance"] == unit_balance(unit(number).pk)
        assert f'data-testid="ledger-row-{number}"' in html
    assert rows["7"]["balance"] == D("277.52")
    assert rows["7"]["oldest_due"] is not None
    for url in ("/money/assessments/", "/money/payments/", "/money/statements/"):
        assert f'href="{url}"' in html
    assert 'data-testid="ledger-record-payment"' in html


def test_ledger_home_forbidden_for_owner(assoc, client):  # noqa: F811
    user = get_user_model().objects.create_user(email="o@example.com")
    client.force_login(user)
    assert client.get("/money/ledger/").status_code in (302, 403)


def test_money_overview_links_ledger(treasurer):
    assert 'href="/money/ledger/"' in treasurer.get("/money/").content.decode()
