"""Owner statements (FIN-04)."""

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from pypdf import PdfReader

from homeoa.access import permissions as perms
from homeoa.access import services as access
from homeoa.access.testing import open_residents
from homeoa.billing.models import AssessmentSchedule, Charge
from homeoa.billing.services import generate_assessments
from homeoa.core import demo
from homeoa.core.context import association_context
from homeoa.ledger.services import unit_balance
from homeoa.letters import pdf as letter_pdf
from homeoa.letters.models import Letter, MailBatch
from homeoa.notify.models import Delivery, Notification
from homeoa.payments.services import record_receipt
from homeoa.plugins import registry
from homeoa.registry import services as reg
from homeoa.registry.models import NoticePreference, Person, Unit
from homeoa.statements import owner_statements as svc
from homeoa.statements.models import Statement, StatementRun

User = get_user_model()
OCT = dt.date(2026, 10, 1)
OCT_END = dt.date(2026, 10, 31)


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


def _unit(n):
    return Unit.objects.get(number=n)


def _consent(unit_number):
    owner = reg.current_owners(_unit(unit_number))[0]
    owner.email = owner.email or f"owner{unit_number.lower()}@example.com"
    owner.save()
    reg.record_notice_preference(
        owner, NoticePreference.Delivery.EMAIL, source=NoticePreference.Source.PORTAL
    )
    return owner


def test_october_closing_equals_subledger(pilot):
    run = svc.generate(pilot, OCT)
    statements = list(run.statements.select_related("unit"))
    assert sorted(s.unit.number for s in statements) == list("ABCDEFGHI")
    for s in statements:
        assert s.closing == unit_balance(s.unit.pk, as_of=OCT_END, association=pilot)
        assert s.closing == s.opening + s.charges - s.payments
    g = run.statements.get(unit__number="G")
    assert g.opening == Decimal("350.86")  # September unpaid
    assert g.charges >= Decimal("350.86")
    assert run.statements.get(unit__number="C").charges == Decimal("396.63")


def test_pdf_contains_statement_table(pilot):
    run = svc.generate(pilot, OCT)
    s = run.statements.get(unit__number="G")
    data = svc.letter_services.letter_bytes(s.letter)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
    assert "Opening balance" in text and "Closing balance" in text
    assert f"{s.closing:,}" in text


def test_ownership_split_mid_month(pilot):
    unit = _unit("D")
    buyer = Person.objects.create(association=pilot, full_name="Łukasz Nguyễn")
    seller_period = reg.ownership_period_on(unit, OCT)
    reg.transfer_ownership(unit, [buyer], dt.date(2026, 10, 16))
    buyer_period = reg.ownership_period_on(unit, OCT_END)
    record_receipt(
        unit=unit,
        amount=Decimal("100.00"),
        received_date=dt.date(2026, 10, 20),
        method="check",
        ownership_period=buyer_period,
    )
    run = svc.generate(pilot, OCT)
    d = list(run.statements.filter(unit=unit).order_by("period_start"))
    assert [(s.period_start, s.period_end) for s in d] == [
        (OCT, dt.date(2026, 10, 15)),
        (dt.date(2026, 10, 16), OCT_END),
    ]
    assert d[0].ownership_period == seller_period and d[1].ownership_period == buyer_period
    assert d[1].owner_names == "Łukasz Nguyễn"
    assert d[1].payments == Decimal("100.00") and d[1].opening == Decimal("0.00")
    assert d[0].charges == Decimal("366.12")
    assert d[0].closing + d[1].closing == unit_balance(unit.pk, as_of=OCT_END, association=pilot)
    # Seller's statement is addressed to the seller.
    assert d[0].letter.recipient_name != "Łukasz Nguyễn"


def test_rerun_is_idempotent_and_keeps_sent(pilot):
    _consent("C")
    run = svc.generate(pilot, OCT)
    first = set(run.statements.values_list("pk", flat=True))
    notes = Notification.objects.count()
    batches = MailBatch.objects.count()
    sent = run.statements.get(unit__number="C")
    sent.status = Statement.Status.SENT
    sent.save()
    run2 = svc.generate(pilot, OCT)
    assert run2.pk == run.pk and StatementRun.objects.count() == 1
    assert run.statements.count() == 9
    assert run.statements.get(unit__number="C").pk == sent.pk
    assert not (first - {sent.pk}) & set(run.statements.values_list("pk", flat=True))
    assert Notification.objects.count() == notes
    assert MailBatch.objects.count() == batches


def test_routing_by_consent_and_preview_for_everyone(pilot):
    priya = _consent("C")
    # A board member who owns a unit still only gets a preview.
    treasurer = User.objects.get(email="treasurer@cedarrow.example")
    Person.objects.filter(user=treasurer).update(user=None)  # seeded on unit B
    owner_a = reg.current_owners(_unit("A"))[0]
    owner_a.user = treasurer
    owner_a.save()
    _consent("A")
    open_residents(pilot)  # still preview-only
    run = svc.generate(pilot, OCT)
    by_unit = {s.unit.number: s for s in run.statements.select_related("unit")}
    assert by_unit["C"].channel == Statement.Channel.EMAIL
    assert by_unit["A"].channel == Statement.Channel.EMAIL
    assert by_unit["B"].channel == Statement.Channel.MAIL
    assert by_unit["B"].mail_batch.status == MailBatch.Status.DRAFT
    assert by_unit["C"].mail_batch is None
    assert set(Delivery.objects.values_list("status", flat=True)) == {Delivery.Status.PREVIEW}
    note = Notification.objects.get(recipient=priya, type_key=svc.NOTIFICATION_TYPE)
    assert note.unit == by_unit["C"].unit
    assert note.deliveries.get().channel == "email"
    mailed = svc.notices(by_unit["B"])
    assert mailed.exists() and all(n.unit == by_unit["B"].unit for n in mailed)


def test_board_owner_without_consent_gets_mail_preview_never_sent(pilot, monkeypatch):
    from homeoa.notify import channels as ch

    sent = []

    class Spy:
        def send(self, delivery):
            sent.append(delivery)
            return "spy"

    monkeypatch.setattr(ch, "get", lambda key: Spy())
    treasurer = User.objects.get(email="treasurer@cedarrow.example")
    owner_a = reg.current_owners(_unit("B"))[0]
    owner_a.user = treasurer
    owner_a.save()
    open_residents(pilot)
    run = svc.generate(pilot, OCT)
    st = run.statements.get(unit__number="B")
    assert st.channel == Statement.Channel.MAIL
    deliveries = Delivery.objects.filter(notification__recipient=owner_a)
    assert deliveries.exists()
    assert set(deliveries.values_list("status", flat=True)) == {Delivery.Status.PREVIEW}
    assert sent == []


def test_font_is_bundled():
    assert (letter_pdf.FONT_DIR / "DejaVuSans.ttf").exists()
    data = letter_pdf.build_pdf(
        letterhead={"name": "Cedar Row"},
        address_lines=["Łukasz Nguyễn"],
        date_text="Oct 1",
        subject="Ünïcode",
        body="ok",
    )
    fonts = PdfReader(io.BytesIO(data)).pages[0]["/Resources"]["/Font"]
    assert any("DejaVu" in str(f.get_object()["/BaseFont"]) for f in fonts.values())


# --- views ------------------------------------------------------------------------------


def _login(client, email):
    client.force_login(User.objects.get(email=email))


def test_pages_and_gating(client, pilot):
    _login(client, "treasurer@cedarrow.example")
    resp = client.get("/money/statements/")
    assert resp.status_code == 200 and b"Generate" in resp.content
    resp = client.post("/money/statements/generate/", {"month": "2026-10"})
    run = StatementRun.objects.get()
    assert resp.status_code == 302 and resp["Location"] == f"/money/statements/{run.pk}/"
    page = client.get(f"/money/statements/{run.pk}/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "Mail" in html and "Preview only" in html
    s = run.statements.first()
    pdf = client.get(f"/money/statements/pdf/{s.pk}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    batch = run.statements.exclude(mail_batch=None).first().mail_batch
    assert client.get(f"/money/statements/{run.pk}/batch/{batch.pk}.pdf").status_code == 200

    client.logout()
    open_residents(pilot)
    _login(client, "ownerc@cedarrow.example")
    assert client.get("/money/statements/").status_code == 403
    assert client.get(f"/money/statements/pdf/{s.pk}.pdf").status_code == 403
    assert client.post("/money/statements/generate/", {"month": "2026-10"}).status_code == 403


def test_generate_needs_manage(client, pilot):
    user = User.objects.create_user("auditor@example.com")
    access.ensure_default_roles(pilot)
    access.assign_role(user, pilot, perms.CPA)
    client.force_login(user)
    assert client.get("/money/statements/").status_code == 200
    assert client.post("/money/statements/generate/", {"month": "2026-10"}).status_code == 403
    assert not Letter.objects.filter(template_version__template__key="statement-cover").exists()
