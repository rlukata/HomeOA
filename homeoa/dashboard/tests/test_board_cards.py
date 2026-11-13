"""Board dashboard cards render with the demo seed and link to their pages."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from homeoa.access.testing import grant
from homeoa.ap.models import Bill, BillLine
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.dashboard import cards
from homeoa.ledger.models import Account, Fund
from homeoa.vendors.models import Vendor

pytestmark = pytest.mark.django_db
TODAY = dt.date(2026, 10, 15)


@pytest.fixture
def seeded(monkeypatch):
    from homeoa.dashboard import views

    monkeypatch.setattr(views, "association_today", lambda a=None: TODAY)
    call_command("seed_demo", "--reset")
    return Association.objects.get()


def _bill(assoc, number, due, status=Bill.Status.APPROVED, amount="100.00"):
    bill = Bill.objects.create(
        vendor=Vendor.objects.first() or Vendor.objects.create(name="Acme"),
        invoice_number=number,
        bill_date=dt.date(2026, 10, 1),
        due_date=due,
        status=status,
    )
    BillLine.objects.create(
        bill=bill,
        account=Account.objects.get(code="5000"),
        fund=Fund.objects.get(code="operating"),
        amount=Decimal(amount),
    )
    return bill


def test_cards_figures(seeded):
    with association_context(seeded):
        due = _bill(seeded, "D1", TODAY + dt.timedelta(days=14))
        _bill(seeded, "LATE", TODAY + dt.timedelta(days=15))
        _bill(seeded, "PAID", TODAY, status=Bill.Status.PAID)
        board = cards.board_cards(seeded, TODAY)
        summary = cards.money_summary(seeded, TODAY)
    assert [b.pk for b in board.ap_due] == [due.pk]
    assert board.ap_due_total == Decimal("100.00")
    assert sum(c for _f, c in board.funds) == summary.cash
    assert {f.code for f, _c in board.funds} >= {"operating", "reserve"}


def test_home_renders_cards_with_links(seeded, client):
    user = get_user_model().objects.create_user(email="dash@example.com")
    grant(user, association=seeded, title="treasurer")
    client.force_login(user)
    body = client.get("/").content.decode()
    for testid in ("dash-cash", "dash-budget", "dash-ap", "dash-ar", "dash-compliance"):
        assert f'data-testid="{testid}"' in body
    for name in ("banking:index", "ap:index", "reports:ar_aging", "compliance:index"):
        assert f'href="{reverse(name)}"' in body
