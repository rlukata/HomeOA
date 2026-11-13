# ruff: noqa: F811
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant
from homeoa.banking import services as banking
from homeoa.banking.models import BankAccount
from homeoa.billing.models import Charge
from homeoa.billing.owner_ledger import owner_ledger
from homeoa.billing.services import generate_assessments
from homeoa.dashboard import cards, todos
from homeoa.ledger.models import Account, Fund
from homeoa.ledger.services import account_balance, unit_balance
from homeoa.payments.models import Receipt
from homeoa.payments.tests.test_services import (  # noqa: F401
    NOV,
    OCT,
    assoc,
    receipt,
    schedule,
    unit,
)
from homeoa.providers.bankfeed import BankTransaction as FeedTxn
from homeoa.registry.models import Occupancy
from homeoa.ui.export import cell, csv_response

D = Decimal
TODAY = dt.date(2026, 10, 15)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for name in list(todos._registered):
        todos.unregister(name)


@pytest.fixture
def fixed_today(monkeypatch):
    from homeoa.dashboard import views
    from homeoa.registry import services as reg

    monkeypatch.setattr(views, "association_today", lambda a=None: TODAY)
    monkeypatch.setattr(reg, "today_for", lambda a=None: TODAY)
    return TODAY


@pytest.fixture
def board(assoc, client, fixed_today):  # noqa: F811
    user = get_user_model().objects.create_user(email="t@example.com")
    grant(user, association=assoc, title="treasurer")
    client.force_login(user)
    return client


@pytest.fixture
def october(assoc, schedule):  # noqa: F811
    """October dues posted; everyone paid except units 7 and 11."""
    generate_assessments(schedule, OCT)
    for n in range(1, 12):
        if n not in (7, 11):
            receipt(n, str(unit_balance(unit(n).pk)))
    return schedule


def _bank(assoc, code="1000", fund="operating", name="Operating"):  # noqa: F811
    return BankAccount.objects.create(
        association=assoc,
        name=name,
        gl_account=Account.objects.get(code=code),
        fund=Fund.objects.get(code=fund),
    )


def _deposit(acct, amount, ext="t1", desc="Zelle from RAFAEL ORTIZ"):
    banking.import_transactions(
        acct,
        [FeedTxn(external_id=ext, posted=dt.date(2026, 10, 6), amount=D(amount), description=desc)],
        source="file",
    )


# ---------------------------------------------------------------- registry + grouping


def _item(id, due=None, **kw):
    return {"id": id, "title": f"Item {id}", "due": due, **kw}


def test_grouping_today_week_upcoming():
    items = [
        todos.TodoItem.from_dict(_item("a")),
        todos.TodoItem.from_dict(_item("b", TODAY - dt.timedelta(days=3))),
        todos.TodoItem.from_dict(_item("c", TODAY + dt.timedelta(days=3))),
        todos.TodoItem.from_dict(_item("d", TODAY + dt.timedelta(days=6))),
        todos.TodoItem.from_dict(_item("e", TODAY + dt.timedelta(days=7))),
    ]
    groups = {g["key"]: [i.id for i in g["items"]] for g in todos.grouped(items, TODAY)}
    assert groups == {"today": ["a", "b"], "week": ["c", "d"], "upcoming": ["e"]}


def test_collect_aggregates_dedupes_and_isolates_failures(assoc):  # noqa: F811
    def boom(a):
        raise RuntimeError("broken provider")

    todos.register("one", lambda a: [_item("x", TODAY), _item("y")])
    todos.register("two", lambda a: [_item("x", TODAY), {"title": "no id"}])
    todos.register("three", boom)
    items = [i for i in todos.collect(assoc) if i.source in ("one", "two", "three")]
    assert [i.id for i in items] == ["y", "x"]  # undated first, then by due date
    assert items[1].source == "one"
    assert items[0].icon == "money"


def test_plugin_provider_used_only_while_enabled(assoc, monkeypatch):  # noqa: F811
    from homeoa.plugins import registry, services

    manifest = SimpleNamespace(id="fakeplug", todos="homeoa.dashboard.tests.test_dashboard.plug")
    monkeypatch.setattr(registry, "all_plugins", lambda: [manifest])
    enabled = {"on": False}
    monkeypatch.setattr(services, "is_enabled", lambda aid, pid: enabled["on"])
    assert not [i for i in todos.collect(assoc) if i.id == "plug-1"]
    enabled["on"] = True
    item = next(i for i in todos.collect(assoc) if i.id == "plug-1")
    assert item.source == "plugin:fakeplug"


def plug(association):
    return [_item("plug-1")]


def test_simplefin_manifest_declares_todos():
    from homeoa_simplefin.manifest import manifest

    assert manifest.todos == "homeoa_simplefin.todos.todo_items"


def test_backup_stale_is_a_todo(assoc, settings, tmp_path):  # noqa: F811
    settings.HOMEOA_BACKUP_STATUS_DIR = str(tmp_path)
    (tmp_path / "last_failure.json").write_text(json.dumps({"time": "2020-01-01T03:00:00+00:00"}))
    items = [i for i in todos.collect(assoc) if i.id == "ops-backup-stale"]
    assert len(items) == 1
    assert "failed" in items[0].sub
    (tmp_path / "last_success.json").write_text(
        json.dumps({"time": dt.datetime.now(dt.UTC).isoformat()})
    )
    assert not [i for i in todos.collect(assoc) if i.id == "ops-backup-stale"]


# ---------------------------------------------------------------- money card


def test_money_summary_from_the_books(assoc, october):  # noqa: F811
    acct = _bank(assoc)
    _deposit(acct, "335.40")
    s = cards.money_summary(assoc, TODAY)
    charges = Charge.objects.filter(period=OCT)
    assert s.dues_period == OCT
    assert s.billed == sum(c.amount for c in charges) == D("3900.00")
    owed = unit_balance(unit(7).pk) + unit_balance(unit(11).pk)
    assert s.collected == s.billed - owed
    assert s.pending_match == D("335.40")
    assert s.operating == account_balance(
        Account.objects.get(code="1000"), fund=Fund.objects.get(code="operating")
    )
    assert s.reserve == D("0.00")
    assert {p.unit.number for p in s.past_due} == {"7", "11"}
    assert s.past_due_total == owed
    assert s.collected_pct == int((s.collected * 100 / s.billed).to_integral_value())


def test_pending_match_capped_and_nothing_past_due_before_due_date(assoc, october, monkeypatch):
    big = SimpleNamespace(amount=D("99999.00"))
    monkeypatch.setattr(cards, "pending_deposits", lambda a: [big])
    due = Charge.objects.filter(period=OCT).first().due_date
    s = cards.money_summary(assoc, due)
    assert s.pending_match == s.outstanding
    assert s.past_due == []


def test_money_summary_empty_books(assoc):  # noqa: F811
    s = cards.money_summary(assoc, TODAY)
    assert (s.billed, s.collected, s.cash, s.collected_pct) == (0, 0, 0, 0)
    assert s.dues_period is None


# ---------------------------------------------------------------- pages


def test_home_renders_groups_and_money_card(board, october):  # noqa: F811
    todos.register("t", lambda a: [_item("soon", TODAY + dt.timedelta(days=2), action="Open")])
    html = board.get("/").content.decode()
    assert "This week" in html and "Item soon" in html
    assert 'data-testid="card-billed">$3,900.00<' in html
    assert 'data-testid="card-past-due"' in html
    assert "/units/7/" in html


def test_url_only_item_is_link_keeping_query(board):
    url = "/money/reports/gl/?account=1900&fund=operating"
    todos.register("q", lambda a: [{"id": "q1", "title": "Q", "url": url, "action": "Review"}])
    html = board.get("/").content.decode()
    assert 'href="/money/reports/gl/?account=1900&amp;fund=operating">Review</a>' in html
    assert 'method="get"' not in html


def test_resident_home_redirects_to_me(assoc, client):  # noqa: F811
    user = get_user_model().objects.create_user(email="o@example.com")
    client.force_login(user)
    resp = client.get("/")
    assert resp.status_code == 302 and resp["Location"].endswith("/me/")


def _pay_unit7_provider(association):
    if Receipt.objects.filter(unit__number="7").exists():
        return []
    return [
        {
            "id": "pay-7",
            "kind": "money",
            "title": "Match deposit: check from unit 7",
            "action": "Confirm",
            "url": "/units/7/",
            "post_url": "/money/payments/new/",
            "post_data": {
                "unit": unit(7).pk,
                "amount": "377.52",
                "received_date": "2026-10-06",
                "method": "check",
                "deposit_account": Account.objects.get(code="1000").pk,
            },
        }
    ]


def test_inline_confirm_removes_item_and_updates_money_card(board, october):  # noqa: F811
    todos.register("pay", _pay_unit7_provider)
    home = board.get("/").content.decode()
    assert 'hx-post="/todos/pay-7/act/"' in home
    before = cards.money_summary(october.association, TODAY).collected
    resp = board.post("/todos/pay-7/act/", HTTP_HX_REQUEST="true")
    html = resp.content.decode()
    assert resp.status_code == 200
    assert "pay-7" not in html
    assert 'id="money-card"' in html and 'hx-swap-oob="true"' in html
    after = cards.money_summary(october.association, TODAY).collected
    assert after == before + D("377.52")
    assert f">{after:,.2f}".replace(">", ">$") in html
    assert unit_balance(unit(7).pk) == 0


def test_act_on_unknown_item_does_nothing(board, october):  # noqa: F811
    resp = board.post("/todos/nope/act/", HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    assert "already done" in resp.content.decode()
    assert not Receipt.objects.filter(unit__number="7").exists()


def test_act_without_htmx_redirects_home(board, october):  # noqa: F811
    todos.register("pay", _pay_unit7_provider)
    resp = board.post("/todos/pay-7/act/")
    assert resp.status_code == 302 and resp["Location"] == "/"


def test_failed_action_reports_error(board, october):  # noqa: F811
    def bad(a):
        return [{**_pay_unit7_provider(a)[0], "post_data": {"unit": unit(7).pk}}]

    todos.register("pay", bad)
    html = board.post("/todos/pay-7/act/", HTTP_HX_REQUEST="true").content.decode()
    assert "could not be done from Home" in html
    assert "pay-7" in html


def test_money_overview(board, october):  # noqa: F811
    html = board.get("/money/").content.decode()
    assert 'data-testid="kpi-billed">$3,900.00<' in html
    assert "Past due by unit" in html


def test_money_overview_forbidden_without_finance(assoc, client):  # noqa: F811
    user = get_user_model().objects.create_user(email="c@example.com")
    grant(user, association=assoc, role="committee")
    client.force_login(user)
    assert client.get("/money/").status_code == 403


# ---------------------------------------------------------------- unit ledger + CSV


def test_owner_ledger_running_balance(assoc, schedule):  # noqa: F811
    generate_assessments(schedule, OCT)
    receipt(7, "100.00")
    generate_assessments(schedule, NOV)
    rows = owner_ledger(unit(7))
    assert [r.charge for r in rows] == [D("377.52"), D("0"), D("377.52")]
    assert [r.payment for r in rows] == [D("0"), D("100.00"), D("0")]
    assert [r.balance for r in rows] == [D("377.52"), D("277.52"), D("655.04")]
    assert rows[-1].balance == unit_balance(unit(7).pk)


def test_unit_page_shows_balance_once_and_ledger(board, october):  # noqa: F811
    html = board.get("/units/7/").content.decode()
    assert html.count('data-testid="unit-balance"') == 1
    assert 'data-testid="unit-balance">$377.52<' in html
    assert "Ledger" in html


def test_units_table_balances_and_csv(board, october):  # noqa: F811
    html = board.get("/units/").content.decode()
    assert html.count('data-testid="unit-balance"') == 11
    resp = board.get("/units/?format=csv")
    assert resp["Content-Type"].startswith("text/csv")
    lines = resp.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Unit,Building,Owner")
    row7 = next(line for line in lines if line.startswith("7,"))
    assert row7.endswith("377.52,377.52")


def test_unit_ledger_csv(board, october):  # noqa: F811
    resp = board.get("/units/8/?format=csv")
    lines = resp.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "Date,Description,Charge,Payment,Balance"
    assert lines[-1].endswith(",0.00")


def test_csv_cells_guard_formulas_and_keep_decimals():
    assert cell("=SUM(A1)") == "'=SUM(A1)"
    assert cell("-1") == "'-1"
    assert cell(D("-50.00")) == "-50.00"
    assert cell(dt.date(2026, 10, 1)) == "2026-10-01"
    assert cell(None) == ""
    resp = csv_response("a b/c", ["x"], [[1]])
    assert 'filename="a-b-c.csv"' in resp["Content-Disposition"]


# ---------------------------------------------------------------- lease on FileStorage


def test_lease_upload_goes_to_file_storage_and_downloads_via_signed_url(
    board,
    assoc,
    settings,
    tmp_path,  # noqa: F811
):
    settings.HOMEOA_FILE_ROOT = str(tmp_path)
    resp = board.post(
        "/units/5/occupants/add/",
        {
            "name": "Chris Doyle",
            "kind": "tenant",
            "start": "2026-07-01",
            "lease_document": SimpleUploadedFile("lease.pdf", b"%PDF-1.4", "application/pdf"),
        },
    )
    assert resp.status_code == 302
    occ = Occupancy.objects.get(person__full_name="Chris Doyle")
    assert occ.lease_key.startswith(f"{assoc.pk}/registry/leases/")
    page = board.get("/units/5/").content.decode()
    assert f"/units/5/occupants/{occ.pk}/lease/" in page
    dl = board.get(f"/units/5/occupants/{occ.pk}/lease/")
    assert dl.status_code == 302 and dl["Location"].startswith("/files/")
    assert board.get(dl["Location"]).content == b"%PDF-1.4"


@pytest.mark.django_db
def test_parallel_run_provider_registered(assoc):
    from homeoa.imports.diff.todos import todo_items

    pairs = dict(todos.providers(assoc))
    assert pairs["parallel_run"] is todo_items


@pytest.mark.django_db
def test_activity_keeps_autopay_run_above_routine_job_rows(assoc):
    from homeoa import audit
    from homeoa.dashboard.views import ACTIVITY_LIMIT, _activity
    from homeoa.payments.models import AutopayRun

    run = AutopayRun.all_associations.create(
        association=assoc, as_of=dt.date(2026, 11, 1), units=3, total=D("900.00")
    )
    audit.record("payments.autopay_collected", run, association=assoc)
    for _i in range(ACTIVITY_LIMIT + 3):
        audit.record("delinquency.stage_changed", run, association=assoc)

    feed = _activity(assoc)
    assert len(feed) == ACTIVITY_LIMIT
    actions = [ev.action for ev in feed]
    assert actions.count("payments.autopay_collected") == 1
    assert any(ev.object_repr == "Autopay collected from 3 units" for ev in feed)


@pytest.mark.django_db
@pytest.mark.parametrize(("age_days", "pinned"), [(-1, True), (35, True), (36, False)])
def test_activity_pins_autopay_only_when_recent(assoc, settings, age_days, pinned):
    from homeoa import audit
    from homeoa.dashboard.views import ACTIVITY_LIMIT, _activity
    from homeoa.payments.models import AutopayRun

    run = AutopayRun.all_associations.create(
        association=assoc, as_of=dt.date(2026, 9, 1), units=3, total=D("900.00")
    )
    ev = audit.record("payments.autopay_collected", run, association=assoc)
    # Audit rows are immutable, so move the fixed clock instead of the event.
    from homeoa.dashboard.views import _local_date

    settings.HOMEOA_FIXED_TODAY = _local_date(ev.created_at, assoc) + dt.timedelta(days=age_days)
    for _i in range(ACTIVITY_LIMIT + 3):
        audit.record("delinquency.stage_changed", run, association=assoc)

    actions = [e.action for e in _activity(assoc)]
    assert ("payments.autopay_collected" in actions) is pinned


def test_collect_filters_personal_items_by_user(assoc):  # noqa: F811
    """Items with ``for_user`` show only to that user; shared items to everyone."""
    todos.register(
        "f11",
        lambda a: [
            {"id": "f11-shared", "title": "Shared"},
            {"id": "f11-mine", "title": "Mine", "for_user": 1},
            {"id": "f11-other", "title": "Other", "for_user": 2},
        ],
    )
    try:

        def ids(user=None):
            return {i.id for i in todos.collect(assoc, user) if i.source == "f11"}

        assert ids(SimpleNamespace(pk=1)) == {"f11-shared", "f11-mine"}
        assert ids() == {"f11-shared", "f11-mine", "f11-other"}
    finally:
        todos.unregister("f11")
