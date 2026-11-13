import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.audit.models import AuditEvent
from homeoa.banking import services
from homeoa.banking.models import (
    BankAccount,
    BankTransaction,
    CsvColumnMapping,
    StatementBalance,
    TransactionImmutableError,
    TxnStatus,
)
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.events import subscribe, unsubscribe
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund, JournalEntry
from homeoa.providers.defaults.fileupload import (
    BankFileError,
    CsvMapping,
    FileUploadFeed,
    Sign,
    guess_mapping,
    parse_money,
)

D = Decimal
HERE = Path(__file__).parent
OFX = (HERE / "fixtures" / "oct.ofx").read_bytes()
E2E_CSV = (HERE.parents[2] / "e2e" / "fixtures" / "bank_oct.csv").read_bytes()


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def acct(assoc):
    return BankAccount.objects.create(
        association=assoc,
        name="Operating checking",
        account_number="000123454321",
        gl_account=Account.objects.get(code="1000"),
        fund=Fund.objects.get(code="operating"),
    )


# ------------------------------------------------------------------ parser


def test_ofx_parse_fitid_amounts_and_balance():
    st = FileUploadFeed().parse_statement("oct.ofx", OFX)
    assert [(t.external_id, t.amount) for t in st.transactions] == [
        ("202610030001", D("335.40")),
        ("202610040001", D("212.18")),
        ("202610050001", D("-612.34")),
    ]
    t = st.transactions[0]
    assert t.description == "Zelle from RAFAEL ORTIZ" and t.posted == dt.date(2026, 10, 3)
    assert isinstance(t.amount, Decimal)
    assert st.balances[0].amount == D("14317.41")
    assert st.balances[0].as_of == dt.date(2026, 10, 6)
    assert st.account_number == "000123454321"


def test_e2e_csv_fixture_guessed():
    txns = FileUploadFeed().parse_file("bank_oct.csv", E2E_CSV)
    assert [(t.description, t.amount) for t in txns] == [
        ("Zelle from RAFAEL ORTIZ", D("396.63")),
        ("KING COUNTY TREAS", D("212.18")),
    ]
    assert all(t.external_id.startswith("h:") for t in txns)


def test_sign_conventions():
    feed = FileUploadFeed()
    out = feed.parse_file(
        "c.csv",
        b"Posted,Payee,Amt\n10/05/2026,CITY UTIL,612.34\n10/06/2026,REFUND,(5.00)\n",
        mapping=CsvMapping(date="Posted", payee="Payee", amount="Amt", sign=Sign.POSITIVE_OUT),
    )
    assert [t.amount for t in out] == [D("-612.34"), D("5.00")]
    assert out[0].description == "CITY UTIL" and out[0].posted == dt.date(2026, 10, 5)
    split = feed.parse_file(
        "s.csv",
        b"Date,Description,Debit,Credit,Balance\n"
        b'2026-10-03,Zelle,,335.40,"14,717.57"\n2026-10-05,Water,612.34,,"14,105.23"\n',
        mapping=guess_mapping(["Date", "Description", "Debit", "Credit", "Balance"]),
    )
    assert [(t.amount, t.balance) for t in split] == [
        (D("335.40"), D("14717.57")),
        (D("-612.34"), D("14105.23")),
    ]


def test_parse_money_exact_and_rejects_garbage():
    assert parse_money("$1,234.50") == D("1234.50")
    assert parse_money("12.30-") == D("-12.30")
    with pytest.raises(BankFileError):
        parse_money("12.345")
    with pytest.raises(BankFileError):
        parse_money("abc")


def test_bad_files_friendly_errors():
    feed = FileUploadFeed()
    with pytest.raises(BankFileError, match="date and amount"):
        feed.parse_file("x.csv", b"Foo,Bar\n1,2\n")
    with pytest.raises(BankFileError, match="Row 2"):
        feed.parse_file("x.csv", b"Date,Amount\nnot-a-date,1.00\n")
    with pytest.raises(BankFileError, match="OFX"):
        feed.parse_file("x.ofx", b"OFXHEADER:100\n<OFX>garbage")
    with pytest.raises(BankFileError, match="empty"):
        feed.parse_file("x.csv", b"")
    with pytest.raises(BankFileError, match="5 MB"):
        feed.parse_file("x.csv", b"a" * (5 * 1024 * 1024 + 1))


# ------------------------------------------------------------------ import + dedupe


def test_import_twice_adds_nothing_and_does_not_post(acct):
    b1 = services.import_file(acct, "oct.ofx", OFX)
    b2 = services.import_file(acct, "oct.ofx", OFX)
    assert (b1.imported_count, b1.duplicate_count) == (3, 0)
    assert (b2.imported_count, b2.duplicate_count) == (0, 3)
    assert BankTransaction.objects.count() == 3
    assert set(BankTransaction.objects.values_list("status", flat=True)) == {"unmatched"}
    assert JournalEntry.objects.count() == 0
    sb = StatementBalance.objects.get()
    assert sb.amount == D("14317.41") and sb.as_of == dt.date(2026, 10, 6)


def test_identical_same_day_rows_both_survive_and_overlap_dedupes(acct):
    a = b"Date,Description,Amount\n2026-10-03,COFFEE,4.00\n2026-10-03,COFFEE,4.00\n"
    services.import_file(acct, "a.csv", a)
    assert BankTransaction.objects.count() == 2
    # Overlapping file: the same two plus a third identical and a new one.
    b = a + b"2026-10-03,COFFEE,4.00\n2026-10-04,TEA,3.00\n"
    batch = services.import_file(acct, "b.csv", b)
    assert (batch.imported_count, batch.duplicate_count) == (2, 2)
    assert BankTransaction.objects.filter(description="COFFEE").count() == 3


def test_csv_mapping_saved_and_reused(acct):
    services.import_file(acct, "bank_oct.csv", E2E_CSV)
    m = CsvColumnMapping.objects.get(bank_account=acct)
    assert (m.date_column, m.amount_column) == ("Date", "Amount")
    m.amount_column, m.sign = "Amt", Sign.POSITIVE_OUT
    m.save()
    services.import_file(acct, "c.csv", b"Date,Description,Amt\n2026-10-05,WATER,612.34\n")
    assert BankTransaction.objects.get(description="WATER").amount == D("-612.34")
    with pytest.raises(BankFileError, match="Amt"):
        services.import_file(acct, "bank_oct.csv", E2E_CSV)


def test_import_audited_and_event(acct):
    got = []
    subscribe("banking.transactions_imported", got.append)
    try:
        batch = services.import_file(acct, "oct.ofx", OFX)
    finally:
        unsubscribe("banking.transactions_imported", got.append)
    assert got[0].imported == 3 and got[0].batch_id == batch.pk
    assert len(got[0].transaction_ids) == 3
    assert AuditEvent.objects.filter(action="bank.import").count() == 1


def test_transactions_immutable_but_status_changes(acct):
    services.import_file(acct, "oct.ofx", OFX)
    t = BankTransaction.objects.first()
    t.amount = D("1.00")
    with pytest.raises(TransactionImmutableError):
        t.save()
    t.refresh_from_db()
    with pytest.raises(TransactionImmutableError):
        t.delete()
    services.set_status(t, TxnStatus.CATEGORIZED)
    t.refresh_from_db()
    assert t.status == "categorized"
    with pytest.raises(ValueError):
        services.set_status(t, "bogus")


def test_account_number_encrypted_last4(acct):
    acct.refresh_from_db()
    assert acct.last4 == "4321" and acct.masked_number == "····4321"
    from django.db import connection

    with connection.cursor() as c:
        c.execute("SELECT account_number FROM banking_bankaccount WHERE id=%s", [acct.pk])
        raw = c.fetchone()[0]
    assert "000123454321" not in raw


def test_dedupe_scoped_per_account(acct, assoc):
    other = BankAccount.objects.create(
        association=assoc,
        name="Reserve savings",
        gl_account=Account.objects.get(code="1010"),
        fund=Fund.objects.get(code="reserve"),
    )
    services.import_file(acct, "oct.ofx", OFX)
    services.import_file(other, "oct.ofx", OFX)
    assert BankTransaction.objects.count() == 6


# ------------------------------------------------------------------ views


@pytest.fixture
def board(client, assoc):
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("treasurer@example.org")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


def test_upload_view_imports_and_lists(board, acct):
    resp = board.get("/money/bank/import/")
    assert resp.status_code == 200
    up = SimpleUploadedFile("bank_oct.csv", E2E_CSV, content_type="text/csv")
    resp = board.post("/money/bank/import/", {"bank_account": acct.pk, "file": up}, follow=True)
    assert resp.status_code == 200
    html = resp.content.decode()
    assert html.count('data-testid="bank-txn"') == 2
    assert "Zelle from RAFAEL ORTIZ" in html and "$396.63" in html


def test_upload_bad_file_shows_error(board, acct):
    up = SimpleUploadedFile("x.csv", b"Foo,Bar\n1,2\n", content_type="text/csv")
    resp = board.post("/money/bank/import/", {"bank_account": acct.pk, "file": up})
    assert resp.status_code == 200
    assert "column mapping" in resp.content.decode()
    assert BankTransaction.objects.count() == 0


def test_account_and_mapping_forms(board, assoc):
    gl = Account.objects.get(code="1000")
    fund = Fund.objects.get(code="operating")
    resp = board.post(
        "/money/bank/accounts/new/",
        {
            "name": "Checking",
            "account_number": "9876 5432 1098",
            "gl_account": gl.pk,
            "fund": fund.pk,
            "active": "on",
        },
    )
    assert resp.status_code == 302
    a = BankAccount.objects.get(name="Checking")
    assert a.last4 == "1098"
    board.post(
        f"/money/bank/accounts/{a.pk}/",
        {
            "name": "Checking 2",
            "account_number": "",
            "gl_account": gl.pk,
            "fund": fund.pk,
            "active": "on",
        },
    )
    a.refresh_from_db()
    assert a.name == "Checking 2" and a.account_number == "9876 5432 1098"
    resp = board.post(
        f"/money/bank/accounts/{a.pk}/mapping/",
        {"date_column": "Posted", "amount_column": "Amt", "sign": "positive_out", "skip_rows": 0},
    )
    assert resp.status_code == 302
    assert a.csv_mapping.to_mapping().sign == Sign.POSITIVE_OUT
    assert "····1098" in board.get("/money/bank/").content.decode()


def test_import_requires_manage_perm(client, assoc):
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("owner@example.org")
    grant(user, role="cpa", association=assoc)
    client.force_login(user)
    assert client.get("/money/bank/import/").status_code == 403
    assert client.get("/money/bank/").status_code == 200


def test_board_member_opens_import_to_propose(client, assoc):
    """Board members who are not the Treasurer open the import screen to propose an import;
    residents never reach it."""
    from homeoa.access.testing import grant

    user = get_user_model().objects.create_user("member@example.org")
    grant(user, association=assoc)
    client.force_login(user)
    assert client.get("/money/bank/").status_code == 200
    assert client.get("/money/bank/import/").status_code == 200
    client.force_login(get_user_model().objects.create_user("owner@example.org"))
    assert client.get("/money/bank/import/").status_code == 403


# ------------------------------------------------------------------ undo + overlap


def test_undo_batch_removes_transactions_and_allows_reimport(acct):
    batch = services.import_file(acct, "oct.ofx", OFX)
    with pytest.raises(services.UndoRefused):
        services.undo_batch(batch, None, "  ")
    services.undo_batch(batch, None, "Wrong account")
    batch.refresh_from_db()
    assert batch.is_undone and batch.undo_reason == "Wrong account"
    assert BankTransaction.objects.count() == 0
    assert StatementBalance.objects.count() == 0
    ev = AuditEvent.objects.get(action="bank.import_undo")
    assert "Wrong account" in str(ev.changes)
    with pytest.raises(services.UndoRefused, match="already"):
        services.undo_batch(batch, None, "again")
    assert services.import_file(acct, "oct.ofx", OFX).imported_count == 3


@pytest.mark.parametrize("status", [TxnStatus.MATCHED, TxnStatus.CATEGORIZED])
def test_undo_refused_once_any_txn_is_matched(acct, status):
    batch = services.import_file(acct, "oct.ofx", OFX)
    services.set_status(BankTransaction.objects.first(), status)
    with pytest.raises(services.UndoRefused, match="matched or categorized"):
        services.undo_batch(batch, None, "oops")
    assert BankTransaction.objects.count() == 3
    batch.refresh_from_db()
    assert not batch.is_undone


def test_undo_view(board, acct):
    batch = services.import_file(acct, "oct.ofx", OFX)
    url = f"/money/bank/imports/{batch.pk}/"
    assert "Undo import" in board.get(url).content.decode()
    resp = board.post(url, {"reason": ""})
    assert "Give a reason" in resp.content.decode()
    resp = board.post(url, {"reason": "Duplicate of CSV"}, follow=True)
    assert "Import undone." in resp.content.decode()
    assert BankTransaction.objects.count() == 0


def test_overlap_warning_between_sources(board, acct):
    services.import_file(acct, "oct.ofx", OFX)  # Oct 3-5
    assert services.overlapping_batches(acct) == []
    services.import_file(acct, "again.ofx", OFX)  # same source, all duplicates: no warning
    assert services.overlapping_batches(acct) == []
    up = SimpleUploadedFile("bank_oct.csv", E2E_CSV, content_type="text/csv")  # Oct 3-4
    resp = board.post("/money/bank/import/", {"bank_account": acct.pk, "file": up}, follow=True)
    html = resp.content.decode()
    pairs = services.overlapping_batches(acct)
    assert len(pairs) == 1 and {b.source for b in pairs[0]} == {"ofx", "csv"}
    assert "Undo one of them" in html and "bank_oct.csv" in html and "oct.ofx" in html
    assert (
        'data-testid="bank-overlap"'
        in board.get(f"/money/bank/accounts/{acct.pk}/").content.decode()
    )
    services.undo_batch(pairs[0][1], None, "dup")
    assert services.overlapping_batches(acct) == []
