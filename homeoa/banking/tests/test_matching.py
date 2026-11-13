"""Deposit matching, categorize, split, interfund, undo, reconciliation."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from homeoa.audit.models import AuditEvent
from homeoa.banking import matching, reconcile, services, todos
from homeoa.banking.models import (
    BankAccount,
    BankTransaction,
    CategorizeRule,
    Match,
    PayerAlias,
    TxnStatus,
)
from homeoa.billing.models import ChargeKind
from homeoa.billing.services.adjustments import manual_charge
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
from homeoa.ledger.services import Line, account_balance, post_entry, unit_balance
from homeoa.payments.models import Receipt, ReceiptStatus
from homeoa.payments.services import record_receipt
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.registry import services as registry
from homeoa.registry.models import Person, Unit

D = Decimal
OCT1 = dt.date(2026, 10, 1)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    load_chart_of_accounts(a)
    with association_context(a):
        yield a


@pytest.fixture
def user(assoc):
    return get_user_model().objects.create_user("treasurer@example.org")


@pytest.fixture
def operating(assoc):
    return BankAccount.objects.create(
        association=assoc,
        name="Operating checking",
        gl_account=Account.objects.get(code="1000"),
        fund=Fund.objects.get(code="operating"),
    )


@pytest.fixture
def reserve(assoc):
    return BankAccount.objects.create(
        association=assoc,
        name="Reserve savings",
        gl_account=Account.objects.get(code="1010"),
        fund=Fund.objects.get(code="reserve"),
    )


def _unit(number, owner):
    unit = Unit.objects.create(number=str(number))
    person = Person.objects.create(full_name=owner)
    registry.set_initial_ownership(unit, [person], dt.date(2020, 1, 1))
    return unit


def _charge(unit, amount, date=OCT1):
    manual_charge(
        unit,
        D(amount),
        "October dues",
        "October dues",
        kind=ChargeKind.ASSESSMENT,
        account="4000",
        date=date,
    )


def post_receipt(unit, amount, *, date, bank_account, user=None):
    return record_receipt(
        unit=unit,
        amount=amount,
        received_date=date,
        method="check",
        deposit_account=bank_account.gl_account,
        fund=bank_account.fund,
        posted_by=user,
    )


@pytest.fixture
def units(assoc):
    u = {
        3: _unit(3, "Mei Chen"),
        7: _unit(7, "Dana Whitfield"),
        11: _unit(11, "Rafael Ortiz"),
    }
    _charge(u[3], "335.40")
    _charge(u[7], "377.52")
    _charge(u[11], "335.40")
    return u


def _import(account, *rows):
    txns = [
        FeedTxn(external_id=f"x{i}-{desc}", posted=date, amount=D(amt), description=desc)
        for i, (date, desc, amt) in enumerate(rows)
    ]
    services.import_transactions(account, txns, source="csv")
    qs = BankTransaction.objects.filter(bank_account=account)
    return [qs.get(external_id=t.external_id) for t in txns]


ORTIZ = (dt.date(2026, 10, 3), "Zelle from RAFAEL ORTIZ", "335.40")
KING = (dt.date(2026, 10, 4), "KING COUNTY TREAS", "212.18")


# ------------------------------------------------------------------ suggestions


def test_ortiz_suggested_to_unit_11_first(operating, units):
    (txn,) = _import(operating, ORTIZ)
    sugg = matching.suggestions(txn)
    assert sugg[0].kind == "unit"
    assert sugg[0].unit == units[11]
    assert sugg[0].reason == "exact amount and name match"
    # Unit 3 owes the same amount but the name doesn't match: ranked lower.
    assert [s.unit for s in sugg if s.kind == "unit"] == [units[11], units[3]]
    assert sugg[1].reason == "exact amount match"


def test_name_only_match_and_normalize(operating, units):
    (txn,) = _import(operating, (OCT1, "ONLINE TRANSFER FROM DANA WHITFIELD", "100.00"))
    assert matching.normalize("Zelle from RAFAEL ORTIZ") == "RAFAEL ORTIZ"
    (top,) = [s for s in matching.suggestions(txn) if s.kind == "unit"]
    assert top.unit == units[7] and top.reason == "name match"


def test_king_county_suggests_other_income(operating, units):
    (txn,) = _import(operating, KING)
    (s,) = matching.suggestions(txn)
    assert s.kind == "categorize" and s.account.code == "4200"


def test_rule_suggestion(operating, units):
    CategorizeRule.objects.create(contains="king county", account=Account.objects.get(code="4300"))
    (txn,) = _import(operating, KING)
    (s,) = matching.suggestions(txn)
    assert s.account.code == "4300" and "king county" in s.reason


def test_category_learned_from_the_same_payee(operating, units, user):
    """A payer categorized before gets the same account suggested, whatever dates and
    reference numbers the bank puts in the text."""
    (first,) = _import(
        operating, (dt.date(2026, 9, 5), "PUGET SOUND ENERGY 0905 REF 4411", "-180.25")
    )
    matching.categorize(first, Account.objects.get(code="5000"), user)
    (txn,) = _import(
        operating, (dt.date(2026, 10, 5), "PUGET SOUND ENERGY 1005 REF 9873", "-192.10")
    )
    (s,) = matching.suggestions(txn)
    assert s.kind == "categorize" and s.account.code == "5000"
    assert s.reason == "same payee, categorized this way on Sep 5, 2026"


def test_learned_category_skips_undone_and_money_the_other_way(operating, units, user):
    """Only categorizations still in force count, and money in never teaches money out."""
    refund, charge = _import(
        operating,
        (dt.date(2026, 9, 5), "ACME SUPPLY 1001", "50.00"),
        (dt.date(2026, 9, 6), "ACME SUPPLY 1002", "-75.00"),
    )
    matching.categorize(refund, Account.objects.get(code="4200"), user)
    matching.categorize(charge, Account.objects.get(code="5200"), user)
    matching.undo(charge, user)
    debit, deposit = _import(
        operating,
        (OCT1, "ACME SUPPLY 1003", "-80.00"),
        (OCT1, "ACME SUPPLY 1004", "20.00"),
    )
    assert not [s for s in matching.suggestions(debit) if s.kind == "categorize"]
    (s,) = matching.suggestions(deposit)
    assert s.account.code == "4200" and s.score == 45  # not the Other income fallback
    # A saved rule still comes first.
    CategorizeRule.objects.create(contains="ACME", account=Account.objects.get(code="4300"))
    (s,) = matching.suggestions(deposit)
    assert s.account.code == "4300" and "ACME" in s.reason


# ------------------------------------------------------------------ confirm


def test_confirm_sets_balance_zero_and_posts_receipt(operating, units, user):
    (txn,) = _import(operating, ORTIZ)
    best = matching.suggestions(txn)[0]
    matching.confirm_units(txn, [(best.unit, txn.amount)], user)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED
    assert unit_balance(units[11].pk) == D("0.00")
    assert account_balance(operating.gl_account) == D("335.40")
    m = Match.objects.get(txn=txn)
    assert m.receipt.source == "bank" and m.receipt.method == "zelle"
    assert m.receipt.allocations.count() == 1  # paid the October charge
    assert m.entry.lines.get(account__code="1100").ownership_period_id is not None
    assert AuditEvent.objects.filter(action="bank.match").exists()
    with pytest.raises(matching.MatchRefused):
        matching.confirm_units(txn, [(units[11], txn.amount)], user)


def test_overpayment_goes_to_prepaid(operating, units, user):
    (txn,) = _import(operating, (OCT1, "Zelle from RAFAEL ORTIZ", "400.00"))
    matching.confirm_units(txn, [(units[11], txn.amount)], user)
    assert unit_balance(units[11].pk) == D("-64.60")
    assert account_balance(Account.objects.get(code="2100")) == D("64.60")


def test_alias_learning(operating, units, user):
    t1, t2 = _import(
        operating,
        (OCT1, "MOBILE DEPOSIT CHAN FAMILY TRUST", "335.40"),
        (dt.date(2026, 11, 2), "MOBILE DEPOSIT CHAN FAMILY TRUST", "10.00"),
    )
    matching.confirm_units(t1, [(units[3], t1.amount)], user)
    alias = PayerAlias.objects.get()
    assert alias.text == "CHAN FAMILY TRUST" and alias.unit == units[3]
    (s,) = [s for s in matching.suggestions(t2) if s.kind == "unit"]
    assert s.unit == units[3] and s.reason == "name match"
    # The owner's own name is not saved as an alias.
    (t3,) = _import(operating, ORTIZ)
    matching.confirm_units(t3, [(units[11], t3.amount)], user)
    assert PayerAlias.objects.count() == 1


def test_split_across_units(operating, units, user):
    (txn,) = _import(operating, (OCT1, "CHECK DEPOSIT", "712.92"))
    with pytest.raises(matching.MatchRefused):
        matching.confirm_units(txn, [(units[3], D("335.40")), (units[7], D("300.00"))], user)
    matching.confirm_units(txn, [(units[3], D("335.40")), (units[7], D("377.52"))], user)
    assert unit_balance(units[3].pk) == 0 and unit_balance(units[7].pk) == 0
    assert Match.objects.filter(txn=txn).count() == 2
    assert account_balance(operating.gl_account) == D("712.92")


def test_link_to_recorded_check_no_double_count(operating, units, user):
    recorded = post_receipt(
        units[7], D("377.52"), date=dt.date(2026, 10, 2), bank_account=operating, user=user
    )
    (txn,) = _import(operating, (dt.date(2026, 10, 5), "DEPOSIT", "377.52"))
    best = matching.suggestions(txn)[0]
    assert best.kind == "link" and best.receipt == recorded and best.unit == units[7]
    matching.link_receipt(txn, recorded, user)
    assert unit_balance(units[7].pk) == 0
    assert account_balance(operating.gl_account) == D("377.52")  # posted once
    # Once linked it isn't offered for another deposit.
    (other,) = _import(operating, (dt.date(2026, 10, 6), "DEPOSIT", "377.52"))
    assert not [s for s in matching.suggestions(other) if s.kind == "link"]


def test_categorize_posts_income_and_expense(operating, units, user):
    king, fee = _import(operating, KING, (OCT1, "SERVICE FEE", "-12.00"))
    other = Account.objects.get(code="4200")
    matching.categorize(king, other, user, save_rule="KING COUNTY")
    assert account_balance(other) == D("212.18")
    assert account_balance(operating.gl_account) == D("212.18")
    assert CategorizeRule.objects.get().account == other
    matching.categorize(fee, Account.objects.get(code="5400"), user)
    assert account_balance(operating.gl_account) == D("200.18")
    king.refresh_from_db()
    assert king.status == TxnStatus.CATEGORIZED
    with pytest.raises(matching.MatchRefused):
        matching.categorize(king, Account.objects.get(code="1100"), user)


def test_undo_reverses(operating, units, user):
    (txn,) = _import(operating, ORTIZ)
    matching.confirm_units(txn, [(units[11], txn.amount)], user)
    matching.undo(txn, user)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.UNMATCHED
    assert unit_balance(units[11].pk) == D("335.40")
    assert account_balance(operating.gl_account) == 0
    m = Match.objects.get()
    assert m.undone_at is not None
    assert m.receipt.status == ReceiptStatus.VOID
    with pytest.raises(matching.MatchRefused):
        matching.undo(txn, user)
    # Can be matched again afterwards.
    matching.confirm_units(txn, [(units[11], txn.amount)], user)


def test_interfund_settlement(operating, reserve, units, user):
    # Monthly reserve contribution booked as interfund.
    post_entry(
        [
            Line(account="5900", fund="operating", debit=D("500.00")),
            Line(account="2900", fund="operating", credit=D("500.00")),
            Line(account="1900", fund="reserve", debit=D("500.00")),
            Line(account="4900", fund="reserve", credit=D("500.00")),
        ],
        date=OCT1,
    )
    assert matching.reserve_transfer_owed(operating.association) == D("500.00")
    (out,) = _import(operating, (dt.date(2026, 10, 5), "TRANSFER TO SAVINGS", "-500.00"))
    (inn,) = _import(reserve, (dt.date(2026, 10, 6), "TRANSFER FROM CHECKING", "500.00"))
    s = [s for s in matching.suggestions(out) if s.kind == "interfund"]
    assert s and s[0].partner == inn
    matching.confirm_interfund(out, s[0].partner, user)
    assert matching.reserve_transfer_owed(operating.association) == 0
    assert account_balance(Account.objects.get(code="1900")) == 0
    assert account_balance(reserve.gl_account) == D("500.00")
    assert account_balance(operating.gl_account) == D("-500.00")
    # Undo from either side clears both.
    matching.undo(inn, user)
    out.refresh_from_db()
    assert out.status == TxnStatus.UNMATCHED
    assert matching.reserve_transfer_owed(operating.association) == D("500.00")


# ------------------------------------------------------------------ reconciliation


def test_reconciliation_math_and_lock(operating, units, user):
    ortiz, king = _import(operating, ORTIZ, KING)
    matching.confirm_units(ortiz, [(units[11], ortiz.amount)], user)
    stmt = dt.date(2026, 10, 31)
    rec = reconcile.start(operating, stmt, D("547.58"), user=user)
    # King County not categorized yet: not cleared, difference shows.
    assert rec.beginning_balance == 0
    assert rec.cleared_total == D("335.40")
    assert rec.difference == D("212.18")
    with pytest.raises(reconcile.ReconcileRefused):
        reconcile.complete(rec, user)
    matching.categorize(king, Account.objects.get(code="4200"), user)
    rec = reconcile.refresh(rec)
    assert rec.difference == 0
    reconcile.complete(rec, user)
    ortiz.refresh_from_db()
    assert ortiz.is_locked
    with pytest.raises(matching.MatchRefused):
        matching.undo(ortiz, user)
    with pytest.raises(reconcile.ReconcileRefused):
        reconcile.start(operating, stmt, D("1.00"))
    nxt = reconcile.start(operating, dt.date(2026, 11, 30), D("547.58"))
    assert nxt.beginning_balance == D("547.58") and nxt.difference == 0


def test_wrong_ending_balance_cannot_complete(operating, units, user):
    rec = reconcile.start(operating, dt.date(2026, 9, 30), D("10.00"), user=user)
    assert rec.difference == D("10.00")
    with pytest.raises(reconcile.ReconcileRefused):
        reconcile.complete(rec, user)


def test_first_beginning_balance_from_books(operating, units, user):
    post_entry(
        [
            Line(account="1000", fund="operating", debit=D("14382.17")),
            Line(account="3000", fund="operating", credit=D("14382.17")),
        ],
        date=dt.date(2026, 7, 31),
    )
    rec = reconcile.start(operating, dt.date(2026, 9, 30), D("14382.17"))
    assert rec.difference == 0


# ------------------------------------------------------------------ to-dos


def test_todo_items(operating, units):
    ortiz, king = _import(operating, ORTIZ, KING)
    items = {i["id"]: i for i in todos.todo_items(operating.association)}
    m = items[f"bank-match-{ortiz.pk}"]
    assert m["title"] == "Match deposit: Zelle from RAFAEL ORTIZ"
    assert "$335.40" in m["sub"] and "Unit 11" in m["sub"]
    assert "exact amount and name match" in m["sub"]
    assert m["action"] == "Confirm" and m["post_data"] == {"unit": units[11].pk}
    c = items[f"bank-categorize-{king.pk}"]
    assert c["title"] == "Categorize deposit: KING COUNTY TREAS"
    assert "Other income" in c["sub"]


# ------------------------------------------------------------------ views


@pytest.fixture
def board(client, assoc):
    from homeoa.access.testing import grant

    u = get_user_model().objects.create_user("t@example.org")
    grant(u, association=assoc, title="treasurer")
    client.force_login(u)
    return client


@pytest.fixture
def reader(client, assoc):
    from homeoa.access.testing import grant

    u = get_user_model().objects.create_user("b@example.org")
    grant(u, association=assoc)
    client.force_login(u)
    return client


def test_views_confirm_categorize_reconcile(board, operating, units):
    ortiz, king = _import(operating, ORTIZ, KING)
    html = board.get("/money/bank/").content.decode()
    assert "exact amount and name match" in html
    resp = board.post(f"/money/bank/txn/{ortiz.pk}/confirm/", {"unit": units[11].pk}, follow=True)
    html = resp.content.decode()
    assert 'data-testid="unit-balance">$0.00' in html
    resp = board.get(f"/money/bank/txn/{king.pk}/categorize/")
    assert "Other income" in resp.content.decode()
    other = Account.objects.get(code="4200")
    resp = board.post(f"/money/bank/txn/{king.pk}/categorize/", {"account": other.pk}, follow=True)
    assert "Categorized as Other income." in resp.content.decode()
    resp = board.post(
        "/money/bank/reconcile/",
        {"bank_account": operating.pk, "statement_date": "2026-10-31", "ending_balance": "547.58"},
        follow=True,
    )
    assert 'data-testid="recon-difference">$0.00' in resp.content.decode()
    rec_url = resp.redirect_chain[-1][0]
    resp = board.post(rec_url, follow=True)
    assert "reconciled through" in resp.content.decode()
    html = board.get(f"/money/bank/txn/{ortiz.pk}/").content.decode()
    assert "disabled" in html and "Undo match" in html


def test_views_split_and_undo(board, operating, units):
    (txn,) = _import(operating, (OCT1, "CHECK DEPOSIT", "712.92"))
    board.post(
        f"/money/bank/txn/{txn.pk}/split/",
        {"unit": [units[3].pk, units[7].pk, ""], "amount": ["335.40", "377.52", ""]},
    )
    assert Match.objects.filter(txn=txn).count() == 2
    board.post(f"/money/bank/txn/{txn.pk}/undo/")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.UNMATCHED


def test_wrong_balance_view_shows_difference(board, operating, units):
    resp = board.post(
        "/money/bank/reconcile/",
        {"bank_account": operating.pk, "statement_date": "2026-09-30", "ending_balance": "5.00"},
        follow=True,
    )
    html = resp.content.decode()
    assert 'data-testid="recon-difference">$5.00' in html
    resp = board.post(resp.redirect_chain[-1][0])
    assert "must be $0.00" in resp.content.decode()


def test_board_member_proposes_bank_work(reader, operating, units):
    """A board member who is not the Treasurer proposes matches and reconciliations; nothing
    changes until another board member approves."""
    from homeoa.access.testing import grant
    from homeoa.changes.models import ChangeRequest

    treasurer = get_user_model().objects.create_user("t@example.org")
    grant(treasurer, association=operating.association, title="treasurer")
    (txn,) = _import(operating, ORTIZ)
    assert reader.get("/money/bank/").status_code == 200
    assert reader.get(f"/money/bank/txn/{txn.pk}/").status_code == 200
    resp = reader.post(f"/money/bank/txn/{txn.pk}/confirm/", {"unit": units[11].pk})
    assert resp.status_code == 302
    txn.refresh_from_db()
    assert txn.status != "matched"
    resp = reader.post(
        "/money/bank/reconcile/",
        {"bank_account": operating.pk, "statement_date": "2026-09-30", "ending_balance": "5"},
    )
    assert resp.status_code in (200, 302)
    assert ChangeRequest.objects.filter(kind="money.bank_match").count() == 1


# ------------------------------------------------------------------ bank vs books


def _opening(amount, date=dt.date(2026, 9, 1)):
    post_entry(
        [
            Line(account="1000", fund="operating", debit=D(amount)),
            Line(account="3000", fund="operating", credit=D(amount)),
        ],
        date=date,
    )


def test_books_agree_with_bank(operating, units, user):
    _opening("1000.00")
    ortiz, king = _import(operating, ORTIZ, KING)
    matching.confirm_units(ortiz, [(units[11], ortiz.amount)], user)
    matching.categorize(king, Account.objects.get(code="4200"), user)
    rec = reconcile.start(operating, dt.date(2026, 10, 31), D("1547.58"), user=user)
    assert rec.difference == 0
    books = reconcile.book_check(rec)
    assert books.book_balance == D("1547.58")
    assert books.outstanding == [] and books.difference == 0
    reconcile.complete(rec, user)  # no note needed


def test_unlinked_manual_receipt_is_book_difference(operating, units, user):
    (ortiz,) = _import(operating, ORTIZ)
    matching.confirm_units(ortiz, [(units[11], ortiz.amount)], user)
    # A check recorded in the books that never reached the bank.
    post_receipt(units[7], D("377.52"), date=dt.date(2026, 10, 20), bank_account=operating)
    rec = reconcile.start(operating, dt.date(2026, 10, 31), D("335.40"), user=user)
    assert rec.difference == 0
    books = reconcile.book_check(rec)
    assert books.book_balance == D("712.92")
    assert [ln.amount for ln in books.outstanding] == [D("377.52")]
    assert books.adjusted == D("335.40") and books.difference == 0
    # Without the outstanding list the books would be off by the check.
    assert books.book_balance - rec.ending_balance == D("377.52")


def test_book_difference_requires_note(operating, units, user):
    _opening("50.00")
    rec = reconcile.start(
        operating, dt.date(2026, 9, 30), D("0.00"), beginning_balance=D("0.00"), user=user
    )
    assert rec.difference == 0
    assert reconcile.book_check(rec).difference == D("-50.00")
    with pytest.raises(reconcile.ReconcileRefused):
        reconcile.complete(rec, user)
    reconcile.complete(rec, user, note="Opening cash entered twice; fixing next month.")
    rec.refresh_from_db()
    assert rec.is_complete and rec.book_note.startswith("Opening cash")
    ev = AuditEvent.objects.filter(action="bank.reconcile").latest("pk")
    assert "Opening cash" in str(ev.changes) and "-50.00" in str(ev.changes)


def test_book_difference_view(board, operating, units):
    post_receipt(units[7], D("377.52"), date=dt.date(2026, 9, 20), bank_account=operating)
    resp = board.post(
        "/money/bank/reconcile/",
        {
            "bank_account": operating.pk,
            "statement_date": "2026-09-30",
            "ending_balance": "0.00",
            "beginning_balance": "0.00",
        },
        follow=True,
    )
    html = resp.content.decode()
    assert 'data-testid="recon-book-difference">-$377.52' in html or "377.52" in html
    url = resp.redirect_chain[-1][0]
    assert "Write a note" in board.post(url).content.decode()
    resp = board.post(url, {"note": "Check not deposited yet"}, follow=True)
    assert "Check not deposited yet" in resp.content.decode()


def test_reserve_account_deposit_posts_interfund(reserve, units, user):
    (txn,) = _import(reserve, (OCT1, "Zelle from RAFAEL ORTIZ", "335.40"))
    matching.confirm_units(txn, [(units[11], txn.amount)], user)
    assert unit_balance(units[11].pk) == 0
    assert account_balance(reserve.gl_account) == D("335.40")
    assert account_balance(Account.objects.get(code="2900"), fund=reserve.fund) == D("335.40")
    op = Fund.objects.get(code="operating")
    assert account_balance(Account.objects.get(code="1900"), fund=op) == D("335.40")
    matching.undo(txn, user)
    assert unit_balance(units[11].pk) == D("335.40")
    assert account_balance(Account.objects.get(code="2900"), fund=reserve.fund) == 0
    assert account_balance(Account.objects.get(code="1900"), fund=op) == 0
    assert Receipt.objects.get().status == ReceiptStatus.VOID


def _lines(code, fund):
    from homeoa.ledger.models import JournalLine

    return JournalLine.objects.filter(account__code=code, fund__code=fund)


def test_reserve_overpayment_credit_consumed_in_operating(reserve, units, user):
    (txn,) = _import(reserve, (OCT1, "Zelle from RAFAEL ORTIZ", "435.40"))
    matching.confirm_units(txn, [(units[11], txn.amount)], user)
    assert unit_balance(units[11].pk) == D("-100.00")
    _charge(units[11], "100.00", date=dt.date(2026, 11, 1))  # consumes the credit
    assert unit_balance(units[11].pk) == 0
    op = Fund.objects.get(code="operating")
    assert account_balance(Account.objects.get(code="2100"), fund=op) == 0
    assert not _lines("2100", "reserve").exists()


def test_refund_of_reserve_overpayment(reserve, units, user):
    from homeoa.payments.services import record_refund

    (txn,) = _import(reserve, (OCT1, "Zelle from RAFAEL ORTIZ", "435.40"))
    matching.confirm_units(txn, [(units[11], txn.amount)], user)
    receipt = Match.objects.get().receipt
    record_refund(
        receipt, amount=D("100.00"), date=OCT1, method="check", reason="overpaid", posted_by=user
    )
    assert account_balance(reserve.gl_account) == D("335.40")
    assert unit_balance(units[11].pk) == 0
    op = Fund.objects.get(code="operating")
    assert account_balance(Account.objects.get(code="2100"), fund=op) == 0
    assert not _lines("2100", "reserve").exists()
    # Interfund still nets: reserve owes operating exactly the cash it kept.
    owed = account_balance(Account.objects.get(code="2900"), fund=reserve.fund)
    due = account_balance(Account.objects.get(code="1900"), fund=op)
    assert owed == due == D("335.40")


@pytest.fixture
def insurance(assoc):
    return BankAccount.objects.create(
        association=assoc,
        name="Insurance savings",
        gl_account=Account.objects.get(code="1020"),
        fund=Fund.objects.get(code="insurance"),
    )


def test_interfund_settlement_any_pair_of_three_funds(operating, reserve, insurance, units, user):
    # The monthly insurance transfer is booked on 2920/1920.
    post_entry(
        [
            Line(account="5910", fund="operating", debit=D("893.08")),
            Line(account="2920", fund="operating", credit=D("893.08")),
            Line(account="1920", fund="insurance", debit=D("893.08")),
            Line(account="4910", fund="insurance", credit=D("893.08")),
        ],
        date=OCT1,
    )
    assoc = operating.association
    assert matching.insurance_transfer_owed(assoc) == D("893.08")
    assert matching.reserve_transfer_owed(assoc) == 0
    (out,) = _import(operating, (dt.date(2026, 10, 5), "TRANSFER TO INS", "-893.08"))
    (inn,) = _import(insurance, (dt.date(2026, 10, 6), "TRANSFER FROM CHECKING", "893.08"))
    matching.confirm_interfund(out, inn, user)
    assert matching.insurance_transfer_owed(assoc) == 0
    assert account_balance(Account.objects.get(code="1920")) == 0
    assert account_balance(Account.objects.get(code="1900")) == 0
    assert account_balance(insurance.gl_account) == D("893.08")
    # Reserve <-> insurance settles too, each fund balanced (post_entry enforces it).
    (r_out,) = _import(reserve, (dt.date(2026, 10, 7), "TO INSURANCE", "-100.00"))
    (i_in,) = _import(insurance, (dt.date(2026, 10, 7), "FROM RESERVE", "100.00"))
    first, _second = matching.confirm_interfund(r_out, i_in, user)
    codes = set(first.entry.lines.values_list("account__code", flat=True))
    assert codes == {"2920", "1010", "1020", "1920"}
    assert account_balance(insurance.gl_account) == D("993.08")
    # Operating <-> reserve still uses the due-to/due-from pair.
    (o_out,) = _import(operating, (dt.date(2026, 10, 8), "TO SAVINGS", "-50.00"))
    (r_in,) = _import(reserve, (dt.date(2026, 10, 8), "FROM CHECKING", "50.00"))
    first, _second = matching.confirm_interfund(o_out, r_in, user)
    codes = set(first.entry.lines.values_list("account__code", flat=True))
    assert codes == {"2900", "1000", "1010", "1900"}
