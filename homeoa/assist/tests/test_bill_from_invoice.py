"""A new bill filled in from an invoice by AI: AI only proposes; code
checks the answer; the person checks and saves; the invoice is attached and the audit
log says where the fields came from."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant
from homeoa.ap.models import Bill
from homeoa.assist import invoices
from homeoa.assist.invoices import InvoiceFields, InvoiceLine
from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.providers.ai import AiProvider
from homeoa.vendors import services as vendor_services
from homeoa.vendors.models import Vendor

User = get_user_model()


class FakeAi(AiProvider):
    name = "fake AI"
    model = "fake-1"

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def read_document(self, document, *, content_type, instructions, schema):
        self.calls.append({"type": content_type, "instructions": instructions, "schema": schema})
        return self.answer


def _answer(**kw):
    base = {
        "vendor": "City of Tukwila Utilities",
        "vendor_match": "City of Tukwila Utilities",
        "invoice_number": "UT-2026-10",
        "invoice_date": "2026-10-01",
        "due_date": "2026-10-25",
        "total": "612.34",
        "lines": [],
        "summary": "Water and sewer, September",
    }
    return InvoiceFields(**{**base, **kw})


@pytest.fixture
def assoc(db, settings, tmp_path):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = Association.objects.create(name="Cedar Row", short_name="CR", timezone="UTC")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def city(assoc):
    v = vendor_services.save_vendor(Vendor(name="City of Tukwila Utilities"))
    Vendor.objects.filter(pk=v.pk).update(default_expense_account=Account.objects.get(code="5000"))
    return Vendor.objects.get(pk=v.pk)


@pytest.fixture
def ai(monkeypatch):
    """A connected AI provider (the fake); every other provider stays as it is."""
    from homeoa import providers
    from homeoa.assist import views

    fake = FakeAi(_answer())
    real = providers.get_provider

    def pick(kind, association=None):
        return fake if kind == "AiProvider" else real(kind, association)

    for module in (providers, invoices, views):
        monkeypatch.setattr(module, "get_provider", pick)
    return fake


@pytest.fixture
def treasurer(assoc):
    u = User.objects.create_user("t@example.org")
    grant(u, association=assoc, start=dt.date(2020, 1, 1), title="treasurer")
    return u


def test_answer_becomes_a_checked_proposal(assoc, city, ai):
    proposal = invoices.read(assoc, b"%PDF fake", "application/pdf", "october.pdf")
    assert "- City of Tukwila Utilities" in ai.calls[0]["instructions"]  # vendor names only
    assert proposal["fields"] == {
        "vendor": city.pk,
        "invoice_number": "UT-2026-10",
        "bill_date": "2026-10-01",
        "due_date": "2026-10-25",
        "description": "Water and sewer, September",
    }
    (line,) = proposal["lines"]
    assert line["account"] == Account.objects.get(code="5000").pk  # the vendor's default
    assert line["amount"] == "612.34" and proposal["warnings"] == []
    assert "Filled in by AI from october.pdf" in proposal["note"]


def test_doubtful_answers_become_warnings(assoc, city, ai):
    ai.answer = _answer(
        vendor="Greenway Landscaping",
        vendor_match="Someone Not On File",
        invoice_date="October 1st",
        total="$1,000.00",
        lines=[
            InvoiceLine(description="Mowing", amount="400"),
            InvoiceLine(description="Leaves", amount="500"),
        ],
    )
    proposal = invoices.read(assoc, b"%PDF fake", "application/pdf", "greenway.pdf")
    assert proposal["fields"]["vendor"] is None and proposal["fields"]["bill_date"] == ""
    text = " ".join(proposal["warnings"])
    assert "“Greenway Landscaping” is not in your vendor list" in text
    assert "invoice date could not be read" in text
    assert "add up to 900.00 but the invoice total is 1000.00" in text
    assert [(ln["memo"], ln["amount"]) for ln in proposal["lines"]] == [
        ("Mowing", "400.00"),
        ("Leaves", "500.00"),
    ]


def test_no_answer_means_fill_in_by_hand(assoc, ai):
    ai.answer = None
    with pytest.raises(invoices.CouldNotRead):
        invoices.read(assoc, b"%PDF fake", "application/pdf", "x.pdf")


def test_add_bill_offers_the_reader_only_with_ai(client, assoc, treasurer):
    client.force_login(treasurer)
    html = client.get("/money/bills/add/").content.decode()
    assert 'data-testid="invoice-reader"' not in html  # no AI provider connected


def test_fill_check_and_submit(client, assoc, city, treasurer, ai):
    client.force_login(treasurer)
    assert 'data-testid="invoice-reader"' in client.get("/money/bills/add/").content.decode()
    invoice = SimpleUploadedFile("october.pdf", b"%PDF-1.4 invoice", "application/pdf")
    resp = client.post("/money/bills/assist/bill-from-invoice/", {"invoice": invoice})
    assert resp.status_code == 302 and "?prefill=" in resp["Location"]
    assert not Bill.objects.exists()  # nothing saved yet
    page = client.get(resp["Location"]).content.decode()
    assert 'data-testid="bill-prefilled"' in page and "UT-2026-10" in page
    assert "october.pdf will be attached" in page
    held = page.split('name="held_file" value="')[1].split('"')[0]
    data = {
        "vendor": city.pk,
        "invoice_number": "UT-2026-10",
        "bill_date": "2026-10-01",
        "due_date": "2026-10-25",
        "description": "Water and sewer, September",
        "held_file": held,
        "lines-TOTAL_FORMS": "3",
        "lines-INITIAL_FORMS": "0",
        "lines-0-account": Account.objects.get(code="5000").pk,
        "lines-0-fund": Fund.objects.get(code="operating").pk,
        "lines-0-amount": "612.34",
        "save": "1",
    }
    resp = client.post("/money/bills/add/", data)
    assert resp.status_code == 302
    bill = Bill.objects.get()
    assert bill.total == Decimal("612.34")
    (attachment,) = bill.attachments.all()
    assert attachment.name == "october.pdf"
    event = AuditEvent.objects.get(action="ap.bill_prefilled")
    assert "AI (fake AI, fake-1) from october.pdf" in event.message


def test_held_file_cannot_be_forged(client, assoc, city, treasurer):
    from homeoa.ap import prefill

    assert prefill.held_file("not-a-token", assoc) is None
    other = Association.objects.create(name="Elsewhere", short_name="EW")
    token = prefill.hold_file(other, "x.pdf", b"%PDF", "application/pdf", "test")
    assert prefill.held_file(token, assoc) is None  # signed for another association
