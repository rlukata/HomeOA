import datetime as dt
from decimal import Decimal

import pytest
from django.db import DatabaseError, connection, transaction
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from homeoa.audit.models import AuditEvent
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.events import subscribe, unsubscribe
from homeoa.ledger.chart import ACCOUNTS, load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry, JournalLine, LedgerImmutableError
from homeoa.ledger.services import (
    EntryAlreadyReversed,
    FundUnbalanced,
    InvalidLine,
    Line,
    PeriodLocked,
    UnbalancedEntry,
    account_balance,
    gl_detail,
    ownership_balance,
    post_entry,
    reverse_entry,
    set_locked_through,
    trial_balance,
    unit_balance,
)
from homeoa.registry.models import OwnershipPeriod, Unit

D = Decimal
OCT1 = dt.date(2026, 10, 1)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Olympia Condo", short_name="TC")
    load_chart_of_accounts(a)
    # Subledger tags are FKs into the registry: tests tag units 1-11 by primary key.
    for n in range(1, 12):
        Unit.all_associations.create(pk=n, association=a, number=str(n))
    periods = ((70, dt.date(2020, 1, 1), dt.date(2026, 9, 30)), (71, dt.date(2026, 10, 1), None))
    for pk, start, end in periods:
        OwnershipPeriod.all_associations.create(
            pk=pk, association=a, unit_id=7, start=start, end=end
        )
    with association_context(a):
        yield a


def charge(unit, amount, date=OCT1, fund="operating"):
    return post_entry(
        [
            Line(account="1100", fund=fund, debit=D(amount), unit_id=unit),
            Line(account="4000", fund=fund, credit=D(amount), unit_id=unit),
        ],
        date=date,
        memo="Dues",
        source_type="test.charge",
        source_id=unit,
    )


def test_chart_seeded_and_idempotent(assoc):
    assert Account.objects.count() == len(ACCOUNTS)
    assert set(Fund.objects.values_list("code", flat=True)) == {"operating", "reserve", "insurance"}
    assert Fund.objects.get(code="reserve").is_reserve
    assert load_chart_of_accounts(assoc) == (0, 0)


def test_post_balanced_entry(assoc):
    entry = charge(3, "335.40")
    assert entry.lines.count() == 2
    assert account_balance(Account.objects.get(code="1100")) == D("335.40")
    assert account_balance(Account.objects.get(code="4000")) == D("335.40")
    assert AuditEvent.objects.filter(action="ledger.post", object_id=str(entry.pk)).exists()


def test_unbalanced_rejected(assoc):
    with pytest.raises(UnbalancedEntry):
        post_entry(
            [
                Line(account="1000", fund="operating", debit=D("10.00")),
                Line(account="4200", fund="operating", credit=D("9.99")),
            ],
            date=OCT1,
        )
    assert JournalEntry.objects.count() == 0


def test_fund_unbalanced_rejected(assoc):
    with pytest.raises(FundUnbalanced):
        post_entry(
            [
                Line(account="1010", fund="reserve", debit=D("100.00")),
                Line(account="1000", fund="operating", credit=D("100.00")),
            ],
            date=OCT1,
        )


def test_interfund_transfer_with_due_to_from(assoc):
    post_entry(
        [
            Line(account="1010", fund="reserve", debit=D("100.00")),
            Line(account="2900", fund="reserve", credit=D("100.00")),
            Line(account="1900", fund="operating", debit=D("100.00")),
            Line(account="1000", fund="operating", credit=D("100.00")),
        ],
        date=OCT1,
    )
    reserve = Fund.objects.get(code="reserve")
    operating = Fund.objects.get(code="operating")
    assert account_balance(Account.objects.get(code="1010"), fund=reserve) == D("100.00")
    assert account_balance(Account.objects.get(code="1010"), fund=operating) == 0
    assert trial_balance(fund=reserve).difference == 0
    assert trial_balance(fund=operating).difference == 0


@pytest.mark.parametrize(
    ("debit", "credit", "exc"),
    [
        (D("-5.00"), D("0"), InvalidLine),
        (D("0"), D("0"), InvalidLine),
        (D("5.00"), D("5.00"), InvalidLine),
        (D("1.005"), D("0"), InvalidLine),
        (5.0, D("0"), InvalidLine),
        (5, D("0"), InvalidLine),
    ],
)
def test_bad_amounts(assoc, debit, credit, exc):
    with pytest.raises(exc):
        post_entry(
            [
                Line(account="1000", fund="operating", debit=debit, credit=credit),
                Line(account="4200", fund="operating", credit=D("5.00")),
            ],
            date=OCT1,
        )


def test_unknown_or_inactive_account(assoc):
    with pytest.raises(InvalidLine):
        post_entry(
            [
                Line(account="9999", fund="operating", debit=D("1.00")),
                Line(account="4200", fund="operating", credit=D("1.00")),
            ],
            date=OCT1,
        )
    Account.objects.filter(code="4200").update(active=False)
    with pytest.raises(InvalidLine):
        post_entry(
            [
                {"account": "1000", "fund": "operating", "debit": D("1.00")},
                {"account": "4200", "fund": "operating", "credit": D("1.00")},
            ],
            date=OCT1,
        )


def test_other_association_account_rejected(assoc):
    other = Association.objects.create(name="Other", short_name="O")
    load_chart_of_accounts(other)
    foreign = Account.all_associations.get(association=other, code="1000")
    with pytest.raises(InvalidLine):
        post_entry(
            [
                Line(account=foreign, fund="operating", debit=D("1.00")),
                Line(account="4200", fund="operating", credit=D("1.00")),
            ],
            date=OCT1,
        )


def test_period_lock(assoc):
    set_locked_through(assoc, dt.date(2026, 9, 30))
    with pytest.raises(PeriodLocked):
        charge(1, "10.00", date=dt.date(2026, 9, 30))
    charge(1, "10.00", date=OCT1)


def test_posted_entry_immutable_in_model(assoc):
    entry = charge(1, "10.00")
    entry.memo = "changed"
    with pytest.raises(LedgerImmutableError):
        entry.save()
    with pytest.raises(LedgerImmutableError):
        entry.delete()
    line = entry.lines.first()
    line.debit = D("11.00")
    with pytest.raises(LedgerImmutableError):
        line.save()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ledger_journalline SET debit = debit + 1 WHERE id = %s",
        "DELETE FROM ledger_journalline WHERE id = %s",
    ],
)
def test_trigger_blocks_line_mutation(assoc, sql):
    line = charge(1, "10.00").lines.first()
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute(sql, [line.pk])


def test_trigger_blocks_entry_mutation(assoc):
    entry = charge(1, "10.00")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("UPDATE ledger_journalentry SET memo = 'x' WHERE id = %s", [entry.pk])


def test_db_rejects_negative_or_two_sided_line(assoc):
    # Everything in one rolled-back savepoint: the unsealed raw entry never reaches commit.
    with pytest.raises(DatabaseError, match="one_sided"), transaction.atomic():
        with connection.cursor() as cur:
            entry = JournalEntry.all_associations.get(pk=_raw_entry(cur, assoc))
        JournalLine.all_associations.bulk_create(
            [
                JournalLine(
                    association=assoc,
                    entry=entry,
                    account=Account.objects.get(code="1000"),
                    fund=Fund.objects.get(code="operating"),
                    debit=D("-1.00"),
                    credit=D("0"),
                )
            ]
        )


def test_reversal_nets_to_zero(assoc):
    entry = charge(4, "377.52")
    rev = reverse_entry(entry, date=dt.date(2026, 10, 5))
    assert rev.reversal_of == entry
    assert unit_balance(4) == 0
    assert account_balance(Account.objects.get(code="4000")) == 0
    assert unit_balance(4, as_of=dt.date(2026, 10, 4)) == D("377.52")
    with pytest.raises(EntryAlreadyReversed):
        reverse_entry(entry)
    with pytest.raises(EntryAlreadyReversed):
        reverse_entry(rev)
    assert AuditEvent.objects.filter(action="ledger.reverse").count() == 1


def test_event_published(assoc):
    seen = []
    subscribe("ledger.entry_posted", seen.append)
    try:
        entry = charge(2, "10.00")
    finally:
        unsubscribe("ledger.entry_posted", seen.append)
    assert seen and seen[-1].entry_id == entry.pk
    assert seen[-1].total == D("10.00")


def test_balance_at_date(assoc):
    charge(1, "100.00", date=dt.date(2026, 9, 1))
    charge(1, "50.00", date=OCT1)
    ar = Account.objects.get(code="1100")
    assert account_balance(ar, as_of=dt.date(2026, 8, 31)) == 0
    assert account_balance(ar, as_of=dt.date(2026, 9, 30)) == D("100.00")
    assert account_balance(ar) == D("150.00")


def test_fund_isolation(assoc):
    charge(1, "100.00", fund="operating")
    charge(1, "30.00", fund="reserve")
    ar = Account.objects.get(code="1100")
    assert account_balance(ar, fund=Fund.objects.get(code="operating")) == D("100.00")
    assert account_balance(ar, fund=Fund.objects.get(code="reserve")) == D("30.00")
    assert account_balance(ar) == D("130.00")


def test_unit_subledger(assoc):
    charge(1, "100.00")
    charge(2, "40.00")
    # A payment by unit 1: credit receivable tagged to the unit.
    post_entry(
        [
            Line(account="1000", fund="operating", debit=D("60.00")),
            Line(account="1100", fund="operating", credit=D("60.00"), unit_id=1),
        ],
        date=OCT1,
    )
    assert unit_balance(1) == D("40.00")
    assert unit_balance(2) == D("40.00")
    # Income lines tagged to the unit do not count toward the unit balance.
    assert unit_balance(3) == 0


def test_trial_balance_and_gl(assoc):
    charge(1, "100.00", date=dt.date(2026, 9, 1))
    charge(1, "50.00")
    tb = trial_balance()
    assert tb.difference == 0
    assert tb.total_debit == D("150.00")
    groups = gl_detail(start=OCT1, account=Account.objects.get(code="1100"))
    (g,) = groups
    assert g.opening == D("100.00")
    assert g.closing == D("150.00")
    assert len(g.rows) == 1


AMOUNTS = st.decimals(min_value=D("0.01"), max_value=D("99999.99"), places=2)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["1000", "1010", "1100", "2000", "4000", "5200", "5900"]),
            st.sampled_from(["1000", "2100", "4000", "4200", "3100", "1900"]),
            st.sampled_from(["operating", "reserve"]),
            AMOUNTS,
        ),
        min_size=1,
        max_size=6,
    )
)
def test_property_trial_balance_stays_zero(assoc, legs):
    lines = []
    for debit_acct, credit_acct, fund, amount in legs:
        lines.append(Line(account=debit_acct, fund=fund, debit=amount))
        lines.append(Line(account=credit_acct, fund=fund, credit=amount))
    entry = post_entry(lines, date=OCT1)
    tb = trial_balance()
    assert tb.difference == 0
    for fund in Fund.objects.all():
        assert trial_balance(fund=fund).difference == 0
    assert all(isinstance(r.debit, Decimal) and isinstance(r.credit, Decimal) for r in tb.rows)
    reverse_entry(entry, date=OCT1)
    assert trial_balance().difference == 0


def test_reversal_defaults_to_association_today(assoc, monkeypatch):
    import homeoa.ledger.services as svc

    assoc.timezone = "Pacific/Kiritimati"  # UTC+14: date differs from UTC most of the day
    assoc.save()
    fixed = dt.datetime(2026, 10, 7, 20, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(svc.timezone, "now", lambda: fixed)
    entry = charge(1, "10.00")
    entry.association.refresh_from_db()
    assert reverse_entry(entry).date == dt.date(2026, 10, 8)


def _raw_line(cur, assoc, entry_id, code, debit, credit):
    cur.execute(
        "INSERT INTO ledger_journalline (association_id, entry_id, account_id, fund_id,"
        " debit, credit, memo) VALUES (%s, %s, %s, %s, %s, %s, '')",
        [
            assoc.pk,
            entry_id,
            Account.objects.get(code=code).pk,
            Fund.objects.get(code="operating").pk,
            debit,
            credit,
        ],
    )


def _raw_entry(cur, assoc):
    cur.execute(
        "INSERT INTO ledger_journalentry (association_id, date, memo, source_type, source_id,"
        " posted_at) VALUES (%s, '2026-10-01', '', '', '', now()) RETURNING id",
        [assoc.pk],
    )
    return cur.fetchone()[0]


def test_db_rejects_balanced_pair_into_sealed_entry(assoc):
    entry = charge(1, "10.00")
    assert entry.sealed_at is not None
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        _raw_line(cur, assoc, entry.pk, "1000", 5, 0)
        _raw_line(cur, assoc, entry.pk, "4200", 0, 5)


def test_db_rejects_unbalanced_unsealed_entry(assoc):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        entry_id = _raw_entry(cur, assoc)
        _raw_line(cur, assoc, entry_id, "1000", 5, 0)
        cur.execute("SET CONSTRAINTS ledger_journalline_balanced IMMEDIATE")


def test_db_rejects_unsealed_entry_at_commit(assoc):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        entry_id = _raw_entry(cur, assoc)
        _raw_line(cur, assoc, entry_id, "1000", 5, 0)
        _raw_line(cur, assoc, entry_id, "4200", 0, 5)
        cur.execute("SET CONSTRAINTS ledger_journalentry_sealed IMMEDIATE")


def test_db_rejects_resealing_or_other_updates(assoc):
    entry = charge(1, "10.00")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        cur.execute("UPDATE ledger_journalentry SET sealed_at = now() WHERE id = %s", [entry.pk])
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cur:
        entry_id = _raw_entry(cur, assoc)
        cur.execute(
            "UPDATE ledger_journalentry SET sealed_at = now(), memo = 'x' WHERE id = %s",
            [entry_id],
        )


def test_post_entry_in_nested_atomic(assoc):
    with transaction.atomic(), transaction.atomic():
        entry = charge(1, "10.00")
        with transaction.atomic():
            charge(2, "20.00")
    with connection.cursor() as cur:
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")  # runs the deferred checks now
    assert JournalEntry.objects.get(pk=entry.pk).sealed_at is not None


def test_overpayment_shows_as_credit(assoc):
    charge(5, "100.00")
    post_entry(
        [
            Line(account="1000", fund="operating", debit=D("150.00")),
            Line(account="1100", fund="operating", credit=D("100.00"), unit_id=5),
            Line(account="2100", fund="operating", credit=D("50.00"), unit_id=5),
        ],
        date=OCT1,
    )
    assert unit_balance(5) == D("-50.00")
    # Credit applied to the next charge.
    charge(5, "100.00", date=dt.date(2026, 11, 1))
    post_entry(
        [
            Line(account="2100", fund="operating", debit=D("50.00"), unit_id=5),
            Line(account="1100", fund="operating", credit=D("50.00"), unit_id=5),
        ],
        date=dt.date(2026, 11, 1),
    )
    assert unit_balance(5) == D("50.00")


def test_ownership_period_split(assoc):
    def owed(unit, period, amount):
        post_entry(
            [
                Line(
                    account="1100",
                    fund="operating",
                    debit=D(amount),
                    unit_id=unit,
                    ownership_period_id=period,
                ),
                Line(account="4000", fund="operating", credit=D(amount), unit_id=unit),
            ],
            date=OCT1,
        )

    owed(7, 70, "120.00")  # seller
    owed(7, 71, "30.00")  # buyer
    assert unit_balance(7) == D("150.00")
    assert unit_balance(7, ownership_period_id=70) == D("120.00")
    assert ownership_balance(71) == D("30.00")
    assert ownership_balance(99) == 0
