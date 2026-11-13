"""Cedar Row demo/sandbox seed (OPS-05). Data lives in ``fixtures/demo/cedar_row.json``.

The seed is a 9-unit condominium: units lettered A-I with unequal allocated interests
(total 100.0000%) and a 2026 budget. All names are fictional.

State after ``seed()`` (what E2E relies on):

* Association "Cedar Row Condominium" in sandbox mode, chart of accounts (operating,
  reserve and insurance funds), units A-I with owners. Renter units (3 of 9, as in the
  pilot): E (tenant Chris Doyle, a seed user), F (Sam Rivera) and H (Jordan Ellis).
* Users: treasurer and secretary (board, MFA not enrolled; the treasurer is also System
  admin, as in the pilot), owner7 (Abdi Warsame, owner of
  unit G only; the address predates the lettering), chris.doyle (tenant of E), ownerc
  (Priya Raman, unit C) and ownera (Maya Nguyen, unit A). See ``USERS``. Live from the first
  posted month, with resident access paused; ``e2e_residents on`` opens it.
* Board logins are owners: treasurer is James Okafor (unit B), secretary Daniel Kim (unit
  D) (``owner_of`` in the fixture; the Person gets the login email; no e-notice consent).
  The maintenance login is Elena Petrova (H), on the Maintenance committee. Governance
  (``governance.demo``): president Rafael Ortiz (I, email president@cedarrow.example, no
  login), vice president Sofia Herrera (E, no login), secretary Kim, treasurer Okafor, terms
  Jan 15 2025 - Dec 31 2026; Landscaping committee chaired by Priya Raman (C) with Elena
  Petrova (H); Maintenance committee chaired by Elena Petrova with Abdi Warsame (G);
  resolutions 2025-01 (Collection policy, PDF owners) and 2025-02 (Gutter cleaning
  contract, PDF board only), both adopted with PDFs filed in the document library.
  One-off maintenance limit $1,000.
* E-notice consent: owners of A and G (``e_delivery`` in the fixture) consented in
  the portal; everyone else gets legal notices by mail. Unit C has no consent on file, so
  the consent flows can be exercised from scratch.
* Dues schedule: $41,188.49 a year, $7,550.00 of it to reserve and $10,717.00 to insurance,
  due on the 10th. Monthly dues come from ``billing.services.unit_dues``, nothing is
  hardcoded here: A, B, C, E, I $396.63; D $366.12; F $381.37; G, H $350.86; $3,432.36 a
  month. Each posted month also posts the reserve ($629.17) and insurance ($893.08)
  interfund transfers. August and September are posted; **October is not**. Everyone paid
  August; everyone but unit G paid September (G owes $350.86); unit H overpaid $50.00
  (credit). No late fee yet.
* Late fee policy "Late fees (placeholder)": $25.00 flat once 10 days past the due
  date (so from Sep 20 for September), no interest, charges due from Aug 1. Nothing is
  assessed until the scheduler runs (E2E runs it for Sep 21, which charges unit G).
* Opening cash (Jul 31) sized so the books show operating $14,382.17, reserve $41,950.00
  and insurance $8,930.00 on Sep 30, once the bank feed's unmatched Sep 30 reserve interest
  ($34.96, SimpleFIN plugin) is categorized; until then reserve is $41,915.04. Without the
  plugin it is $41,950.00. The pilot has no reserve study; these balances are made up.
* Bank accounts operating ··4321, reserve ··8765 and insurance ··2468, no imported bank
  transactions (E2E uploads the Ortiz Zelle for unit I and the King County deposit itself).
* Default letter templates (``letters.services.ensure_default_templates``).
* Delinquency ladder offsets (demo placeholders, unverified, set as audited board
  overrides): reminder 15, late notice 30, demand 60 days after the due date. Unit G
  (due Sep 10) has its reminder due from Sep 25, so by the fixed date Oct 7.
* ``budget_2026`` in the fixture holds the 2026 budget lines for the budget module; it is
  not posted. ``budget_lines()`` reads it.

``bare=True`` stops after the association and board users.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction

from homeoa import audit
from homeoa.core import sandbox
from homeoa.core.context import association_context
from homeoa.core.models import Association

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "demo" / "cedar_row.json"
SOURCE = "seed_demo"

# Published in ``seed_demo --help``.
USERS = {
    "treasurer@cedarrow.example": "board (Treasurer) and System admin, MFA not enrolled",
    "secretary@cedarrow.example": "board (Secretary), MFA not enrolled",
    "maintenance@cedarrow.example": "Elena Petrova (unit H), Maintenance committee",
    "owner7@cedarrow.example": "Abdi Warsame, owner of unit G only (owes September)",
    "ownerc@cedarrow.example": "Priya Raman, owner of unit C",
    "ownera@cedarrow.example": "Maya Nguyen, owner of unit A",
    "chris.doyle@cedarrow.example": "tenant of unit E",
}

# Tables a reset keeps: schema bookkeeping, not data.
_KEEP_TABLES = {"django_migrations", "django_content_type", "auth_permission"}


class SeedRefused(Exception):
    pass


def load_data() -> dict:
    return json.loads(FIXTURE.read_text())


def budget_lines(data: dict | None = None) -> dict[str, list[tuple[str, Decimal]]]:
    """The 2026 budget lines by section, for the budget module."""
    budget = (data or load_data())["budget_2026"]
    return {
        section: [(name, Decimal(amount)) for name, amount in rows]
        for section, rows in budget.items()
        if not section.startswith("_")
    }


def _d(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def check_safe() -> None:
    """Refuse when any association exists that is not a sandbox."""
    for a in Association.objects.all():
        if not sandbox.is_sandbox(a):
            raise SeedRefused(
                f"Association {a.name!r} is not in sandbox mode; seed_demo only replaces "
                "sandbox data."
            )


def wipe() -> None:
    """Empty every data table (only after ``check_safe``). TRUNCATE skips row triggers.

    Refuses (``SeedRefused``) unless ``HOMEOA_ALLOW_DEMO_RESET`` is on in these settings.
    """
    if not getattr(settings, "HOMEOA_ALLOW_DEMO_RESET", False):
        raise SeedRefused("Refusing to wipe: HOMEOA_ALLOW_DEMO_RESET is off in these settings.")
    tables = [
        t
        for t in connection.introspection.django_table_names(only_existing=True)
        if t not in _KEEP_TABLES
    ]
    if not tables:
        return
    quoted = ", ".join(connection.ops.quote_name(t) for t in tables)
    with connection.cursor() as cur:
        cur.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")


@transaction.atomic
def seed(*, bare: bool = False, data: dict | None = None) -> Association:
    data = data or load_data()
    info = data["association"]
    association = Association.objects.create(
        name=info["name"],
        short_name=info["short_name"],
        timezone=info["timezone"],
        fiscal_year_start_month=info["fiscal_year_start_month"],
    )
    sandbox.enable(association)
    with audit.audit_context(label="command:seed_demo"), association_context(association):
        _board_users(association, data)
        _modules(association)
        _status(association, data)
        if not bare:
            _populate(association, data)
    return association


def _modules(association) -> None:
    """The demo shows every optional module, so turn them all on, each after the modules
    it needs."""
    from homeoa.plugins import modules, registry

    done: set[str] = set()

    def turn_on(manifest):
        if manifest.id in done:
            return
        done.add(manifest.id)
        for needed in manifest.requires:
            turn_on(registry.module(needed))
        modules.set_module(association, manifest.id, True, user=None)

    for manifest in registry.all_modules():
        turn_on(manifest)


def _status(association, data) -> None:
    """Live from the first month of billing history, with resident access paused until
    ``e2e_residents on``."""
    from . import status
    from .context import association_today

    periods = data.get("posted_periods") or []
    go_live = _d(min(periods)) if periods else association_today(association)
    status.set_go_live_date(association, go_live, reason="Demo seed")
    status.pause_residents(
        association, reason="Demo: resident access stays closed until e2e_residents on."
    )


def _user(email: str):
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    return user or User.objects.create_user(email)


def _board_users(association, data) -> None:
    from homeoa.access.services import assign_role, ensure_default_roles

    ensure_default_roles(association)
    for spec in data["users"]:
        assign_role(_user(spec["email"]), association, spec["role"], title=spec["title"])


def _populate(association, data) -> None:
    from homeoa.ledger.chart import load_chart_of_accounts

    load_chart_of_accounts(association)
    owners: dict = {}
    units = _registry(association, data, owners)
    schedule = _schedule(association, data)
    _opening_cash(association, data, units)
    _history(association, data, schedule, units)
    _bank_accounts(association, data)
    _late_fee_policy(association)
    _letters(association)
    _delinquency(association)
    _rule_pack(association)
    _simplefin(association)
    _stripe(association)
    _ocr(association)
    _ai(association)
    _budget(association, data)
    _annual_meeting(association)
    _vendors(association)
    _documents(association)
    _governance(association, owners)
    _limits(association)


def _limits(association) -> None:
    from decimal import Decimal

    from homeoa.maintenance import oneoff

    oneoff.set_limit(association, Decimal("1000.00"), reason="Demo: the pilot's one-off limit")


def _budget(association, data) -> None:
    from homeoa.budget.demo import seed_budget

    seed_budget(association, budget_lines(data))


def _annual_meeting(association) -> None:
    """Pilot demo: the Dec 3 annual meeting with the 2027 draft budget."""
    import datetime as dt

    from homeoa.budget import services as budget_services
    from homeoa.meetings import annual
    from homeoa.meetings import services as meeting_services

    current = budget_services.adopted_for(association, 2026)
    budget_services.create_budget(association, 2027, copy_from=current)
    meeting = meeting_services.create_meeting(
        association,
        kind="owners_annual",
        date=dt.date(2026, 12, 3),
        start_time=dt.time(19, 0),
        title="Annual owners meeting",
    )
    annual.workflow_for(meeting)


def _vendors(association) -> None:
    from homeoa.vendors import demo as vendor_demo

    vendor_demo.seed(association)

    from homeoa.maintenance import demo as maintenance_demo

    maintenance_demo.seed(association)


def _documents(association) -> None:
    from homeoa.documents import demo as documents_demo

    documents_demo.seed(association)

    from homeoa.insurance import demo as insurance_demo

    insurance_demo.seed(association)


def _governance(association, owners) -> None:
    from homeoa.governance import demo as governance_demo

    governance_demo.seed(association, owners)


def _letters(association) -> None:
    from homeoa.letters.services import ensure_default_templates

    ensure_default_templates(association)


# Demo-only board overrides, not legal values: the generic pack leaves these unset.
DEMO_DELINQUENCY_OFFSETS = {"reminder": 15, "late_notice": 30, "demand": 60}


def _delinquency(association) -> None:
    from homeoa import rules

    for step, days in DEMO_DELINQUENCY_OFFSETS.items():
        rules.set_override(
            association,
            f"delinquency.{step}_days",
            days,
            reason="demo placeholder, unverified",
        )


def _rule_pack(association) -> None:
    """Select the WA rule pack when its plugin is loaded (HOMEOA_PLUGINS)."""
    from homeoa import providers
    from homeoa.plugins import registry
    from homeoa.plugins.services import save_installation

    if registry.get("rulepack_us_wa") is None:
        return
    save_installation(association, "rulepack_us_wa", enabled=True, data={})
    providers.set_choice(association, "RulePack", "rulepack_us_wa")


def _registry(association, data, owners: dict | None = None) -> dict:
    from homeoa.registry.models import Person, Unit
    from homeoa.registry.services import add_occupancy, set_initial_ownership

    start, lease = _d(data["ownership_start"]), _d(data["tenancy_start"])
    # Board logins sit on the board as owners: link each to its unit's owner Person.
    board_logins = {u["owner_of"]: u["email"] for u in data["users"] if u.get("owner_of")}
    units = {}
    owners = {} if owners is None else owners
    for spec in data["units"]:
        unit = Unit.objects.create(
            association=association,
            number=spec["number"],
            building=spec["building"],
            allocated_interest=Decimal(spec["share"]),
        )
        email = spec.get("owner_email", "")
        login = email or board_logins.get(spec["number"], "")
        owner = Person.objects.create(
            association=association,
            full_name=spec["owner"],
            # contact_email: an address on file without a login (the president, so all
            # three board reviewers get ARC reminder email).
            email=login or spec.get("contact_email", ""),
            user=_user(login) if login else None,
        )
        owners[spec["number"]] = owner
        set_initial_ownership(unit, [owner], start)
        if spec.get("e_delivery") and email:
            # Demo owners on e-delivery consented in the portal (never by import).
            from homeoa.registry import consent
            from homeoa.registry.models import NoticePreference
            from homeoa.registry.services import record_notice_preference

            record_notice_preference(
                owner,
                NoticePreference.Delivery.EMAIL,
                e_delivery_consent=True,
                source=NoticePreference.Source.PORTAL,
                note="demo seed",
                consent_text_version=consent.VERSION,
            )
        if spec.get("tenant"):
            email = spec.get("tenant_email", "")
            tenant = Person.objects.create(
                association=association,
                full_name=spec["tenant"],
                email=email,
                user=_user(email) if email else None,
            )
            add_occupancy(unit, tenant, lease)
        units[spec["number"]] = unit
    return units


def _schedule(association, data):
    from homeoa.billing.models import AssessmentSchedule
    from homeoa.billing.services import default_schedule_kwargs

    spec = data["schedule"]
    return AssessmentSchedule.objects.create(
        association=association,
        name=spec["name"],
        annual_amount=Decimal(spec["annual_amount"]),
        reserve_annual_amount=Decimal(spec["reserve_annual_amount"]),
        insurance_annual_amount=Decimal(spec["insurance_annual_amount"]),
        due_day=spec["due_day"],
        effective_start=_d(spec["effective_start"]),
        **default_schedule_kwargs(association),
    )


def _payments_total(data, schedule, units) -> Decimal:
    from homeoa.billing.services import preview

    total = Decimal("0.00")
    for p in data["payments"]:
        for row in preview(schedule, _d(p["period"])).rows:
            if row.unit.number not in p["except"]:
                total += row.amount
    return total + sum((Decimal(c["amount"]) for c in data["credits"]), Decimal("0.00"))


def _opening_cash(association, data, units) -> None:
    """Opening fund balances, sized so cash on ``cash_as_of`` equals ``funds``."""
    from homeoa.billing.models import AssessmentSchedule
    from homeoa.ledger.services import Line, post_entry

    schedule = AssessmentSchedule.objects.get()
    operating = Decimal(data["funds"]["operating"]) - _payments_total(data, schedule, units)
    # Cash the bank feed brings in (Sep 30 reserve interest) is left out of the opening
    # balance: it arrives unmatched, and categorizing it brings the books to ``funds``.
    reserve = Decimal(data["funds"]["reserve"]) - _feed_total("8765")
    insurance = Decimal(data["funds"]["insurance"]) - _feed_total("2468")
    post_entry(
        [
            Line(account="1000", fund="operating", debit=operating),
            Line(account="3000", fund="operating", credit=operating),
            Line(account="1010", fund="reserve", debit=reserve),
            Line(account="3100", fund="reserve", credit=reserve),
            Line(account="1020", fund="insurance", debit=insurance),
            Line(account="3200", fund="insurance", credit=insurance),
        ],
        date=_d(data["opening_date"]),
        memo="Opening balances (demo)",
        source_type=SOURCE,
        source_id="opening",
        association=association,
    )


def _history(association, data, schedule, units) -> None:
    from homeoa.billing.models import Charge
    from homeoa.billing.services import generate_assessments
    from homeoa.payments.services import record_receipt

    for period in data["posted_periods"]:
        generate_assessments(schedule, _d(period), source=SOURCE)

    extra = {(c["date"], c["unit"]): Decimal(c["amount"]) for c in data["credits"]}
    # Receipts go through the payments service: it allocates to the charge and puts any
    # excess (unit H's $50.00) in prepaid assessments (2100).
    for p in data["payments"]:
        charges = Charge.objects.filter(period=_d(p["period"])).exclude(
            unit__number__in=p["except"]
        )
        for charge in charges.select_related("unit").order_by("pk"):
            amount = charge.amount + extra.get((p["date"], charge.unit.number), Decimal("0"))
            record_receipt(
                unit=charge.unit,
                amount=amount,
                received_date=_d(p["date"]),
                method=p["method"],
                reference=f"{p['reference']}-{charge.unit.number}",
                memo=f"Dues payment · unit {charge.unit.number}",
                source=SOURCE,
            )


def _feed_total(last4: str) -> Decimal:
    """Sum of the fake SimpleFIN transactions for account ``last4`` (0 if plugin is off)."""
    from django.apps import apps

    if not apps.is_installed("homeoa_simplefin"):
        return Decimal("0.00")
    from homeoa_simplefin.fake_server import ACCOUNTS

    return sum(
        (
            Decimal(t["amount"])
            for acct in ACCOUNTS
            if acct["id"].endswith(last4)
            for t in acct["transactions"]
        ),
        Decimal("0.00"),
    )


def _simplefin(association) -> None:
    """Connect SimpleFIN and link its accounts by last 4 (sandbox only).

    Uses SimpleFIN's real demo bridge when ``HOMEOA_SIMPLEFIN_SANDBOX_REAL_BRIDGE`` is on
    and a demo setup token is configured (claimed once); otherwise the fake bridge.
    """
    from django.apps import apps

    # Plugin not enabled (HOMEOA_PLUGINS): the demo simply has no bank feed. Check the app
    # registry, not ImportError: the package may be installed without its app loaded.
    if not apps.is_installed("homeoa_simplefin"):
        return
    from homeoa_simplefin import sync as sf
    from homeoa_simplefin.demo import connect_demo
    from homeoa_simplefin.fake_server import SETUP_TOKEN, FakeClient
    from homeoa_simplefin.models import SimpleFinAccount

    from homeoa.banking.models import BankAccount

    real = connect_demo(association)
    if not real:
        sf.connect(association, SETUP_TOKEN, client=FakeClient())
    for link in SimpleFinAccount.all_associations.filter(association=association):
        bank = BankAccount.all_associations.filter(
            association=association, last4=link.sf_id[-4:]
        ).first()
        if bank is not None:
            sf.map_account(link, bank)
    sf.sync(association, client=None if real else FakeClient())


#: Test-only webhook secret for the fake Stripe provider (sandbox/demo, never a real key).
STRIPE_FAKE_WEBHOOK_SECRET = "whsec_homeoa_fake_demo"  # noqa: S105


def _stripe(association) -> None:
    """Enable the fake Stripe ACH provider when its app is loaded (no real money)."""
    from django.apps import apps

    if not apps.is_installed("homeoa_stripe"):
        return
    from homeoa.plugins.services import save_installation

    save_installation(
        association,
        "stripe",
        enabled=True,
        data={"webhook_secret": STRIPE_FAKE_WEBHOOK_SECRET},
    )
    from homeoa_stripe.demo import seed_bank_accounts

    seed_bank_accounts(association, ("A", "C"))


def _ocr(association) -> None:
    """Read scanned documents with Tesseract on the server when its plugin is loaded;
    without the program installed, scans simply stay unread."""
    from homeoa.plugins import registry
    from homeoa.plugins.services import save_installation

    if registry.get("tesseract") is None:
        return
    save_installation(association, "tesseract", enabled=True, data={"language": "eng"})


def _ai(association) -> None:
    """Connect Claude for AI assistance when its plugin is loaded. Dev and E2E use
    the fake client; elsewhere it stays unavailable until someone adds an API key."""
    from homeoa.plugins import registry
    from homeoa.plugins.services import save_installation

    if registry.get("anthropic") is None:
        return
    save_installation(association, "anthropic", enabled=True, data={"model": "claude-opus-5-5"})


def _late_fee_policy(association) -> None:
    """Placeholder terms ($25 flat after a 10-day grace, no interest) until the board
    adopts real ones. Fees are left to the scheduled job."""
    from homeoa.billing.models import LateFeePolicy
    from homeoa.billing.services.latefees import default_policy_kwargs

    kwargs = default_policy_kwargs(association)
    kwargs.update(
        name="Late fees (placeholder)",
        grace_days=10,
        flat_amount=Decimal("25.00"),
        interest_apr=Decimal("0"),
        effective_start=dt.date(2026, 8, 1),
    )
    LateFeePolicy.objects.create(association=association, **kwargs)


def _bank_accounts(association, data) -> None:
    from homeoa.banking.models import BankAccount
    from homeoa.ledger.models import Account, Fund

    for spec in data["bank_accounts"]:
        BankAccount.objects.create(
            association=association,
            name=spec["name"],
            institution=spec["institution"],
            last4=spec["last4"],
            gl_account=Account.objects.get(code=spec["gl"]),
            fund=Fund.objects.get(code=spec["fund"]),
        )
