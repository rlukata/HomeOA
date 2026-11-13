import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from homeoa.access.testing import grant
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.imports import engine, services
from homeoa.imports.diff import services as diff
from homeoa.imports.diff import todos
from homeoa.imports.diff.models import DiffLine, DiffRun
from homeoa.imports.models import ImportRun
from homeoa.ledger.models import JournalLine

D = Decimal
FIX = Path(__file__).parents[1] / "fixtures"
BASE = FIX / "econdo_sample"
LATER = FIX / "econdo_sample_later_unit7_latefee"
AS_OF = dt.date(2026, 10, 15)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def uploads(folder):
    return [(p.name, p.read_bytes()) for p in sorted(folder.glob("*.csv"))]


def run(assoc, folder, mode):
    from homeoa.ledger.chart import load_chart_of_accounts

    load_chart_of_accounts(assoc)
    r = services.create_run(assoc, source="econdo", mode=mode, as_of=AS_OF, uploads=uploads(folder))
    report = engine.commit(r)
    assert not report["problems"], report["problems"]
    r.refresh_from_db()
    return r


@pytest.fixture
def imported(assoc):
    run(assoc, BASE, ImportRun.Mode.COMMIT)
    return assoc


def test_same_snapshot_is_clean(imported):
    snap = run(imported, BASE, ImportRun.Mode.COMPARISON)
    result = diff.run_diff(snap)
    assert result.is_clean
    assert not result.lines.exists()
    assert result.units_matched == 11


def test_late_fee_snapshot_shows_one_unit_and_one_account(imported):
    snap = run(imported, LATER, ImportRun.Mode.COMPARISON)
    before = JournalLine.objects.count()
    result = diff.run_diff(snap)
    assert JournalLine.objects.count() == before  # the live ledger is unchanged
    units = list(result.lines.filter(kind=DiffLine.Kind.UNIT))
    assert [(u.key, u.difference) for u in units] == [("7", D("25.00"))]
    counted = list(result.lines.filter(kind=DiffLine.Kind.ACCOUNT, counted=True))
    assert [(a.key, a.homeoa, a.econdo, a.difference) for a in counted] == [
        ("4100", D("25.00"), D("50.00"), D("25.00"))
    ]
    # Receivable differs too but is shown as covered by the owner balances.
    assert result.lines.filter(kind=DiffLine.Kind.ACCOUNT, counted=False, key="1100").exists()
    assert result.units_matched == 10
    assert not result.is_clean


def test_fund_imbalance_warning(imported):
    snap = run(imported, LATER, ImportRun.Mode.COMPARISON)
    result = diff.run_diff(snap)
    assert any("Reserve" in w and "3000" in w for w in result.warnings)


def test_explain_and_drilldown(imported):
    snap = run(imported, LATER, ImportRun.Mode.COMPARISON)
    result = diff.run_diff(snap)
    user = get_user_model().objects.create_user("t@example.org")
    unit = result.lines.get(kind=DiffLine.Kind.UNIT)
    drill = diff.drilldown(unit)
    assert any(r["type"] == "Late Fee" and r["amount"] == D("25.00") for r in drill["econdo"])
    acct = result.lines.get(kind=DiffLine.Kind.ACCOUNT, key="4100")
    assert any(r["amount"] == D("25.00") for r in diff.drilldown(acct)["econdo"])
    diff.explain(unit, "Late fee charged by the management company", user)
    diff.explain(acct, "Late fee", user)
    unit.refresh_from_db()
    assert unit.explained and unit.explained_by == user
    assert result.is_clean


def test_tolerance_is_zero():
    row = diff.Row("1", "", D("10.00"), D("10.01"))
    assert row.differs
    assert not diff.Row("1", "", D("10.00"), D("10.00")).differs


def test_only_comparison_snapshots(imported):
    commit_run = ImportRun.objects.filter(mode=ImportRun.Mode.COMMIT).first()
    with pytest.raises(ValueError):
        diff.run_diff(commit_run)


def test_todo(imported):
    assert todos.todo_items(imported, today=dt.date(2026, 10, 1))
    snap = run(imported, BASE, ImportRun.Mode.COMPARISON)
    diff.run_diff(snap)
    from django.utils import timezone

    assert todos.todo_items(imported, today=timezone.localdate()) == []


def test_todo_without_econdo(assoc):
    assert todos.todo_items(assoc) == []


@pytest.fixture
def treasurer(client, imported):
    user = get_user_model().objects.create_user("treasurer@example.org")
    grant(user, association=imported, title="treasurer")
    client.force_login(user)
    return client


def test_views_flow(treasurer):
    files = [
        SimpleUploadedFile(p.name, p.read_bytes(), content_type="text/csv")
        for p in sorted(LATER.glob("*.csv"))
    ]
    resp = treasurer.post(
        "/money/reports/parallel-run/",
        {"source": "econdo", "as_of": AS_OF.isoformat(), "files": files},
    )
    assert resp.status_code == 302, resp.content.decode()[:2000]
    page = treasurer.get("/money/reports/parallel-run/").content.decode()
    assert "Run diff" in page
    snap = ImportRun.objects.get(mode=ImportRun.Mode.COMPARISON)
    resp = treasurer.post(f"/money/reports/parallel-run/run/{snap.pk}/")
    assert resp.status_code == 302
    html = treasurer.get(resp["Location"]).content.decode()
    assert "Owner balances" in html and "GL accounts" in html
    assert "All other 10 units matched." in html
    line = DiffLine.objects.get(kind=DiffLine.Kind.UNIT)
    resp = treasurer.post(f"/money/reports/parallel-run/line/{line.pk}/", {"note": "Late fee"})
    assert resp.status_code == 302
    line.refresh_from_db()
    assert line.note == "Late fee"
    assert "Differences" in treasurer.get("/money/reports/parallel-run/").content.decode()
    assert DiffRun.objects.count() == 1


def test_owner_forbidden(client, imported):
    user = get_user_model().objects.create_user("o@example.org")
    client.force_login(user)
    assert client.get("/money/reports/parallel-run/").status_code == 403


def test_audited(imported):
    from homeoa.audit.models import AuditEvent

    snap = run(imported, LATER, ImportRun.Mode.COMPARISON)
    user = get_user_model().objects.create_user("a@example.org")
    result = diff.run_diff(snap, user=user)
    ev = AuditEvent.objects.get(action="imports.diff_run")
    assert ev.actor == user
    line = result.lines.filter(kind=DiffLine.Kind.UNIT).first()
    diff.explain(line, "Late fee", user)
    ev = AuditEvent.objects.get(action="imports.diff_explain")
    assert ev.actor == user
    assert ev.changes["note"] == ["", "Late fee"]


# ------------------------------------------------------------------ insurance mapping

INSURANCE_ROWS = [
    ("6100", "Transfer to Insurance Reserves", "Expense", "Operating"),
    ("4600", "Insurance Assessments", "Income", "Operating"),
    ("6200", "Insurance Expense", "Expense", "Operating"),
]
INSURANCE_TARGETS = {
    "6100": ("5910", "operating"),
    "4600": ("4910", "insurance"),
    "6200": ("5300", "insurance"),
}


def test_insurance_names_map_in_diff(assoc):
    from homeoa.imports.profiles import default_config
    from homeoa.ledger.chart import load_chart_of_accounts

    load_chart_of_accounts(assoc)
    snap = ImportRun(
        association=assoc,
        source="econdo",
        config=default_config("econdo"),
        snapshot={
            "accounts": [
                {"code": c, "name": n, "type": t, "fund": f} for c, n, t, f in INSURANCE_ROWS
            ],
            "trial_balance": [{"code": "6200", "name": "Insurance Expense"}],
        },
    )
    targets = diff.account_targets(snap)
    assert {k: (t.code, t.fund) for k, t in targets.items()} == INSURANCE_TARGETS
    # A run saved before account_names existed still gets the built-in names.
    snap.config = {k: v for k, v in snap.config.items() if k != "account_names"}
    assert diff.account_targets(snap)["6200"].code == "5300"


def test_insurance_names_map_on_import(assoc, tmp_path):
    from homeoa.imports.models import AccountMapping

    for p in BASE.glob("*.csv"):
        (tmp_path / p.name).write_bytes(p.read_bytes())
    with (tmp_path / "chart_of_accounts.csv").open("a") as f:
        for row in INSURANCE_ROWS:
            f.write(",".join(row) + "\n")
    run(assoc, tmp_path, ImportRun.Mode.COMMIT)
    got = {
        m.external_code: (m.account.code, m.fund.code)
        for m in AccountMapping.objects.filter(external_code__in=INSURANCE_TARGETS)
    }
    assert got == INSURANCE_TARGETS
