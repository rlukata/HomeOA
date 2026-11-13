import datetime as dt
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command

from homeoa.access.services import is_board_member, roles_for, units_for
from homeoa.accounts.models import User
from homeoa.billing.models import BillingRun, Charge
from homeoa.core import demo, sandbox
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.models import Account
from homeoa.ledger.services import account_balance, trial_balance, unit_balance
from homeoa.registry.models import Unit

pytestmark = pytest.mark.django_db

SEP30 = dt.date(2026, 9, 30)


def seeded():
    call_command("seed_demo", "--reset")
    return Association.objects.get()


def test_balances_match_sample():
    a = seeded()
    assert sandbox.is_sandbox(a)
    with association_context(a):
        units = {u.number: u for u in Unit.objects.all()}
        assert len(units) == 9
        assert sum(u.allocated_interest for u in units.values()) == Decimal("100.0000")
        bal = {n: unit_balance(u.pk) for n, u in units.items()}
        assert bal["G"] == Decimal("350.86")  # September unpaid, no late fee yet
        assert bal["H"] == Decimal("-50.00")
        assert all(v == 0 for n, v in bal.items() if n not in ("G", "H"))
        assert account_balance(Account.objects.get(code="1000"), as_of=SEP30) == Decimal("14382.17")
        # Sep 30 interest ($34.96) arrives via the bank feed, unmatched, not in the opening.
        assert account_balance(Account.objects.get(code="1010"), as_of=SEP30) == Decimal("41915.04")
        assert trial_balance().difference == 0
        periods = set(BillingRun.objects.values_list("period", flat=True))
        assert periods == {dt.date(2026, 8, 1), dt.date(2026, 9, 1)}  # October unposted
        assert Charge.objects.filter(period=dt.date(2026, 9, 1)).count() == 9


def test_late_fee_placeholder_policy():
    from homeoa.billing.models import LateFee, LateFeePolicy

    a = seeded()
    with association_context(a):
        policy = LateFeePolicy.objects.get()
        assert "placeholder" in policy.name and policy.active
        assert policy.grace_days == 10
        assert policy.flat_amount == Decimal("25.00")
        assert policy.interest_apr == 0
        assert not LateFee.objects.exists()


def test_simplefin_accounts_linked():
    from homeoa_simplefin.models import SimpleFinAccount

    from homeoa.banking.models import BankAccount
    from homeoa.dashboard.cards import _last_sync

    a = seeded()
    links = SimpleFinAccount.all_associations.filter(association=a)
    assert {link.bank_account.last4 for link in links} == {
        "4321",
        "8765",
    }  # the fake feed has no insurance account
    unsynced = BankAccount.all_associations.filter(
        association=a, last_synced_at__isnull=True, last4__in=["4321", "8765"]
    )
    assert not unsynced.exists()
    assert _last_sync(a) is not None


def test_users_and_roles():
    a = seeded()
    treasurer = User.objects.get(email="treasurer@cedarrow.example")
    secretary = User.objects.get(email="secretary@cedarrow.example")
    assert is_board_member(treasurer, a) and is_board_member(secretary, a)
    owner7 = User.objects.get(email="owner7@cedarrow.example")
    tenant = User.objects.get(email="chris.doyle@cedarrow.example")
    assert not is_board_member(owner7, a) and not is_board_member(tenant, a)
    assert not roles_for(owner7, a)  # Residents off after seeding
    assert units_for(owner7, a) and units_for(tenant, a)
    priya = User.objects.get(email="ownerc@cedarrow.example")
    assert [u.number for u in units_for(priya, a)] == ["C"]


@pytest.mark.django_db(transaction=True)  # reset needs committed seed rows (deferred triggers)
def test_reset_is_idempotent():
    seeded()
    a = seeded()
    assert Association.objects.count() == 1
    with association_context(a):
        assert Unit.objects.count() == 9
        assert unit_balance(Unit.objects.get(number="G").pk) == Decimal("350.86")


def test_bare():
    call_command("seed_demo", "--reset", "--bare")
    a = Association.objects.get()
    assert sandbox.is_sandbox(a)
    with association_context(a):
        assert not Unit.objects.exists()
        assert not Account.objects.exists()
    assert User.objects.filter(email="treasurer@cedarrow.example").exists()


def test_refuses_non_sandbox_association():
    Association.objects.create(name="Real HOA", short_name="RH")
    with pytest.raises(CommandError, match="not in sandbox mode"):
        call_command("seed_demo", "--reset")
    assert Association.objects.get().name == "Real HOA"


def test_requires_reset_when_seeded():
    seeded()
    with pytest.raises(CommandError, match="--reset"):
        call_command("seed_demo")


def test_help_lists_users():
    from homeoa.core.management.commands.seed_demo import Command

    for email in demo.USERS:
        assert email in Command.help


def test_banner(client):
    a = seeded()
    client.force_login(User.objects.get(email="treasurer@cedarrow.example"))
    resp = client.get("/")
    assert b'data-testid="sandbox-banner"' in resp.content or resp.status_code in (301, 302)
    a.settings = {}
    a.save()
    resp = client.get("/", follow=True)
    assert b'data-testid="sandbox-banner"' not in resp.content


def test_banner_warns_when_email_is_really_sent(client, settings):
    seeded()
    client.force_login(User.objects.get(email="treasurer@cedarrow.example"))
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    page = client.get("/", follow=True).content.decode()
    assert "email is really sent" in page
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    page = client.get("/", follow=True).content.decode()
    assert "nothing real is sent" in page


def test_reset_refused_without_setting(settings):
    seeded_once = seeded()
    settings.HOMEOA_ALLOW_DEMO_RESET = False
    with pytest.raises(CommandError, match="HOMEOA_ALLOW_DEMO_RESET"):
        call_command("seed_demo", "--reset")
    assert Association.objects.get() == seeded_once


def test_receipts_and_allocations():
    from homeoa.payments.models import Allocation, Receipt

    a = seeded()
    with association_context(a):
        sep = Receipt.objects.filter(received_date=dt.date(2026, 9, 4))
        assert sep.count() == 8
        assert Allocation.objects.filter(receipt__in=sep).count() >= 8
        unit_g = Charge.objects.get(period=dt.date(2026, 9, 1), unit__number="G")
        assert unit_g.open_balance == Decimal("350.86")
        assert account_balance(Account.objects.get(code="2100")) == Decimal("50.00")
        assert account_balance(Account.objects.get(code="1000"), as_of=SEP30) == Decimal("14382.17")


def test_seed_without_simplefin_plugin(monkeypatch):
    """HOMEOA_PLUGINS unset: the package is importable but its app isn't loaded; skip the feed."""
    from django.apps import apps

    real = apps.is_installed
    monkeypatch.setattr(
        apps, "is_installed", lambda name: name != "homeoa_simplefin" and real(name)
    )
    a = seeded()
    from homeoa_simplefin.models import SimpleFinAccount

    assert not SimpleFinAccount.all_associations.filter(association=a).exists()
    with association_context(a):
        reserve = account_balance(Account.objects.get(code="1010"), as_of=SEP30)
    assert reserve == Decimal("41950.00")


def test_interest_categorized_reaches_funds():
    from homeoa.banking import matching
    from homeoa.banking.models import BankTransaction

    a = seeded()
    with association_context(a):
        txn = BankTransaction.objects.get(amount=Decimal("34.96"))
        assert txn.status == "unmatched"
        board = User.objects.filter(email__startswith="treasurer").first()
        matching.categorize(txn, Account.objects.get(code="4300"), board)
        assert account_balance(Account.objects.get(code="1010"), as_of=SEP30) == Decimal("41950.00")


def test_pilot_dues_and_transfers():
    from homeoa.billing.models import AssessmentSchedule
    from homeoa.billing.services import preview, unit_dues

    a = seeded()
    with association_context(a):
        schedule = AssessmentSchedule.objects.get()
        assert schedule.insurance_annual_amount == Decimal("10717.00")
        numbers = {u.pk: u.number for u in Unit.objects.all()}
        dues = {numbers[pk]: amount for pk, amount in unit_dues(schedule).items()}
        expected = {"D": "366.12", "F": "381.37", "G": "350.86", "H": "350.86"}
        for n in "ABCDEFGHI":
            assert dues[n] == Decimal(expected.get(n, "396.63"))
        october = preview(schedule, dt.date(2026, 10, 1))
        assert sum(r.amount for r in october.rows) == Decimal("3432.36")
        for run in BillingRun.objects.all():
            assert run.reserve_entry_id and run.insurance_entry_id
        cash_ins = account_balance(Account.objects.get(code="1020"), as_of=SEP30)
        assert cash_ins == Decimal("8930.00")
        # Aug + Sep insurance transfers: 2 x $893.08.
        assert account_balance(Account.objects.get(code="4910")) == Decimal("1786.16")


def test_letters_and_budget_seeded():
    from homeoa.letters.models import LetterTemplate

    a = seeded()
    with association_context(a):
        assert LetterTemplate.objects.exists()
    budget = demo.budget_lines()
    assert dict(budget["income"])["Homeowner Assessment"] == Decimal("41188.49")
    assert sum(v for _n, v in budget["operating_expense"]) == Decimal("22921.66")


def test_delinquency_offsets_put_unit_g_on_the_ladder(settings, tmp_path):
    from homeoa import rules
    from homeoa.statements import delinquency

    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    a = seeded()
    with association_context(a):
        offsets = rules.delinquency_offsets(a)
        assert all(r.source == "override" and not r.verified for r in offsets.values())
        result = delinquency.run(a, dt.date(2026, 10, 7))
        assert {(x.case.unit.number, x.step) for x in result.actions} == {("G", "reminder")}
        assert {c.unit.number for c in result.opened} == {"G"}


def test_delinquency_placeholder_offsets_reach_unit_g():
    from homeoa.statements import delinquency

    a = seeded()
    with association_context(a):
        assert delinquency.offsets(a) == demo.DEMO_DELINQUENCY_OFFSETS
        result = delinquency.run(a, dt.date(2026, 10, 7))
        assert [(x.case.unit.number, x.step) for x in result.actions] == [("G", "reminder")]


def test_adopted_budget_matches_dues_schedule_without_new_schedule():
    from homeoa.billing.models import AssessmentSchedule
    from homeoa.budget import services as budget_services

    a = seeded()
    with association_context(a):
        budget = budget_services.adopted_for(a, 2026)
        assert budget is not None
        plan = budget_services.plan_dues(budget)
        schedule = AssessmentSchedule.objects.get()
        assert plan.schedule.annual_amount == schedule.annual_amount
        assert plan.schedule.reserve_annual_amount == schedule.reserve_annual_amount
        assert plan.schedule.insurance_annual_amount == schedule.insurance_annual_amount
        assert plan.conflicts == [schedule]


def test_governance_seeded_and_board_logins_linked():
    from homeoa.documents.models import Document
    from homeoa.governance import services as gov
    from homeoa.governance.models import Committee, Resolution
    from homeoa.registry.models import Person

    a = seeded()
    with association_context(a):
        treasurer = Person.objects.get(user__email="treasurer@cedarrow.example")
        secretary = Person.objects.get(user__email="secretary@cedarrow.example")
        assert (treasurer.full_name, secretary.full_name) == ("James Okafor", "Daniel Kim")
        offices = {t.person.full_name: t.office for t in gov.roster(a, SEP30)}
        assert offices == {
            "Rafael Ortiz": "president",
            "Sofia Herrera": "vice_president",
            "Daniel Kim": "secretary",
            "James Okafor": "treasurer",
        }
        for c in Committee.objects.all():
            assert c.members.filter(is_chair=True).count() == 1
        maintenance = Committee.objects.get(name="Maintenance committee")
        chair = maintenance.members.get(is_chair=True).person
        assert chair.user.email == "maintenance@cedarrow.example"
        rs = list(Resolution.objects.order_by("seq"))
        assert [r.number for r in rs] == ["2025-01", "2025-02"]
        assert all(r.status == "adopted" and r.document_id for r in rs)
        docs = {d.pk: d.visibility for d in Document.objects.filter(governing_kind="resolution")}
        assert docs[rs[0].document_id] == "owners"
        assert docs[rs[1].document_id] == "board"
        assert gov.next_seq(a, 2026) == 1


def test_demo_documents_are_pdfs_with_text_to_search():
    """The demo's documents and resolutions are short PDFs of made-up text, so search finds
    words inside them; the declaration's 1994 page is a scan that only OCR can read."""
    from homeoa.documents import services as documents
    from homeoa.documents import text
    from homeoa.documents.models import DocumentVersion, TextStatus

    a = seeded()
    secretary = User.objects.get(email="secretary@cedarrow.example")
    with association_context(a):
        versions = list(DocumentVersion.objects.select_related("document"))
        for v in versions:
            text.read(v)
            v.refresh_from_db()
        assert len(versions) == 12
        assert not [v.document.title for v in versions if v.text_status == TextStatus.FAILED]
        declaration = next(v for v in versions if v.document.title.startswith("Declaration"))
        scan, amendment = declaration.pages.order_by("number")
        assert scan.ocr or scan.text == ""  # no text layer: only OCR reads it
        assert "parking space" in amendment.text and not amendment.ocr
        for query, title in [
            ("quiet hours", "Rules & regulations 2025"),
            ("pest control", "Budget 2026"),
            ("delinquency schedule", "Collection policy"),
        ]:
            found = [d.title for d in documents.visible_documents(secretary, a, query=query)]
            assert any(title in t for t in found), (query, found)
