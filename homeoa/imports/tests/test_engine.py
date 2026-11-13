import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.imports import engine, services
from homeoa.imports.models import AccountMapping, ImportRun, MappingProfile
from homeoa.ledger.models import Account, JournalEntry
from homeoa.ledger.services import trial_balance, unit_balance
from homeoa.registry.models import Occupancy, OwnershipPeriod, Person, Unit
from homeoa.registry.services import current_notice_preference, current_owners

D = Decimal
FIX = Path(__file__).parents[1] / "fixtures"
BASE = FIX / "appfolio_sample"
LATER = FIX / "appfolio_sample_later_unit7_latefee"
AS_OF = dt.date(2026, 10, 15)


@pytest.fixture
def assoc(db):
    a = Association.objects.create(name="Cedar Row", short_name="CR")
    with association_context(a):
        yield a


def uploads(folder, skip=()):
    return [(p.name, p.read_bytes()) for p in sorted(folder.glob("*.csv")) if p.name not in skip]


def new_run(assoc, folder=BASE, mode=ImportRun.Mode.COMMIT, source="appfolio", **kw):
    return services.create_run(
        assoc, source=source, mode=mode, as_of=AS_OF, uploads=uploads(folder, **kw)
    )


def do_import(assoc, folder=BASE):
    run = new_run(assoc, folder)
    report = engine.commit(run)
    assert report["problems"] == [], report["problems"]
    return report


def balances(assoc):
    return {u.number: unit_balance(u.pk, association=assoc) for u in Unit.objects.all()}


def test_sample_import_matches_data_js(assoc):
    report = do_import(assoc)
    assert report["totals"]["units"] == 11
    assert Unit.objects.count() == 11
    b = balances(assoc)
    assert b["7"] == D("780.04")
    assert b["8"] == D("-50.00")
    assert b["11"] == D("335.40")
    assert b["3"] == D("335.40")
    assert sum(b.values()) == D("1400.84")
    unit7 = Unit.objects.get(number="7")
    assert unit7.building == "B"
    assert unit7.city == "Olympia" and unit7.postal_code == "98501"
    assert unit7.allocated_interest == D("9.68")
    assert [p.full_name for p in current_owners(unit7, AS_OF)] == ["Abdi Warsame"]
    tenants = Occupancy.objects.filter(kind="tenant")
    assert sorted(o.person.full_name for o in tenants) == ["Chris Doyle", "Jordan Ellis"]
    pref = current_notice_preference(Person.objects.get(full_name="Abdi Warsame"))
    # The AppFolio flag is kept as a note, never as consent.
    assert pref.delivery == "postal" and pref.source == "import"
    assert not pref.e_delivery_consent and pref.note == "Imported e-delivery: yes"


def test_opening_balance_posting_balances_trial_balance(assoc):
    do_import(assoc)
    tb = trial_balance(as_of=AS_OF, association=assoc)
    assert tb.difference == 0
    a = {a.code: a for a in Account.objects.all()}
    from homeoa.ledger.services import account_balance

    assert account_balance(a["1000"]) == D("14382.17")
    assert account_balance(a["1010"]) == D("41950.00")
    assert account_balance(a["1100"]) == D("1450.84")
    assert account_balance(a["2100"]) == D("50.00")
    assert account_balance(a["4100"]) == D("25.00")
    assert account_balance(a["4000"]) == D("11700.00")
    opening = JournalEntry.objects.get(source_type=engine.OPENING)
    assert opening.date == dt.date(2026, 7, 31)
    assert JournalEntry.objects.filter(source_type=engine.HISTORY).count() > 50


def test_reimport_changes_nothing(assoc):
    do_import(assoc)
    entries = JournalEntry.objects.count()
    run = new_run(assoc)
    report = engine.preview(run)
    assert report["change_count"] == 0, report["changes"]
    report = engine.commit(run)
    assert report["change_count"] == 0
    assert JournalEntry.objects.count() == entries


def test_dry_run_writes_nothing(assoc):
    run = new_run(assoc)
    report = engine.preview(run)
    assert report["problems"] == []
    assert report["counts"]["units"]["created"] == 11
    assert report["change_count"] > 0
    assert Unit.objects.count() == 0
    assert Person.objects.count() == 0
    assert JournalEntry.objects.count() == 0
    assert AccountMapping.objects.count() == 0
    run.refresh_from_db()
    assert run.status == ImportRun.Status.PREVIEWED


def test_later_snapshot_posts_only_the_late_fee(assoc):
    do_import(assoc)
    report = do_import(assoc, LATER)
    hist = [c for c in report["changes"] if c["section"] == "history"]
    assert len(hist) == 1 and hist[0]["action"] == "created"
    assert report["counts"]["opening"]["unchanged"] == 1
    assert balances(assoc)["7"] == D("805.04")


def test_changed_field_updates_record_and_shows_in_report(assoc, tmp_path):
    do_import(assoc)
    for p in BASE.glob("*.csv"):
        text = p.read_text()
        if p.name == "unit_directory.csv":
            text = text.replace("7,B,", "7,C,")
        (tmp_path / p.name).write_text(text)
    report = do_import(assoc, tmp_path)
    assert report["change_count"] == 1
    change = report["changes"][0]
    assert change["section"] == "units" and change["fields"]["building"] == ["B", "C"]
    assert Unit.objects.get(number="7").building == "C"


def test_changed_history_amount_reverses_and_reposts(assoc, tmp_path):
    do_import(assoc)
    for p in BASE.glob("*.csv"):
        text = p.read_text()
        if p.name == "owner_ledger.csv":
            text = text.replace(
                "7,09/11/2026,Late Fee,Late fee,25.00,,402.52,",
                "7,09/11/2026,Late Fee,Late fee,30.00,,407.52,",
            )
        (tmp_path / p.name).write_text(text)
    report = do_import(assoc, tmp_path)
    actions = {(c["section"], c["action"]) for c in report["changes"]}
    assert ("history", "updated") in actions
    # The owner balance file still says 780.04, so the opening entry absorbs the difference.
    assert balances(assoc)["7"] == D("780.04")
    assert trial_balance(as_of=AS_OF, association=assoc).difference == 0


def test_validation_errors_point_to_file_row_column(assoc, tmp_path):
    for p in BASE.glob("*.csv"):
        text = p.read_text()
        if p.name == "owner_ledger.csv":
            text = text.replace(
                "7,09/11/2026,Late Fee,Late fee,25.00", "7,09/31/2026,Late Fee,Late fee,25.0x"
            )
        (tmp_path / p.name).write_text(text)
    run = new_run(assoc, tmp_path)
    report = engine.commit(run)
    probs = {(p["file"], p["column"]) for p in report["problems"]}
    assert ("owner_ledger.csv", "Date") in probs
    assert ("owner_ledger.csv", "Charge") in probs
    row = next(p for p in report["problems"] if p["column"] == "Date")["row"]
    lines = (tmp_path / "owner_ledger.csv").read_text().splitlines()
    assert "09/31/2026" in lines[row - 1]
    run.refresh_from_db()
    assert run.status == ImportRun.Status.FAILED
    assert Unit.objects.count() == 0


def test_unknown_unit_is_an_error_and_nothing_is_written(assoc, tmp_path):
    for p in BASE.glob("*.csv"):
        text = p.read_text()
        if p.name == "tenant_directory.csv":
            text += "99,Ghost,,,01/01/2026,\n"
        (tmp_path / p.name).write_text(text)
    report = engine.commit(new_run(assoc, tmp_path))
    assert any(
        "99" in p["message"] and p["file"] == "tenant_directory.csv" for p in report["problems"]
    )
    assert Unit.objects.count() == 0 and JournalEntry.objects.count() == 0


def test_account_mapping_is_saved_and_reused(assoc):
    do_import(assoc)
    m = AccountMapping.objects.get(external_code="1200")
    assert m.account.code == "1100" and m.fund.code == "operating"
    assert AccountMapping.objects.get(external_code="1010").fund.code == "reserve"
    # Remap 4900 and check the next run starts from the saved choice.
    m = AccountMapping.objects.get(external_code="4900")
    m.account = Account.objects.get(code="4300")
    m.save()
    run = new_run(assoc)
    assert run.config["accounts"]["4900"]["account"] == "4300"


def test_saved_profile_is_used_by_next_upload(assoc):
    run = new_run(assoc)
    run.config["columns"]["units"]["building"] = ""
    services.save_profile(run, "My AppFolio")
    run2 = new_run(assoc)
    assert run2.profile == MappingProfile.objects.get(name="My AppFolio")
    assert run2.config["columns"]["units"]["building"] == ""


def test_comparison_mode_stores_snapshot_without_touching_books(assoc):
    run = new_run(assoc, mode=ImportRun.Mode.COMPARISON)
    report = engine.commit(run)
    assert report["problems"] == []
    run.refresh_from_db()
    assert run.status == ImportRun.Status.STORED
    assert len(run.snapshot["units"]) == 11
    assert run.snapshot["balances"][6]["balance"] == "780.04"
    assert Unit.objects.count() == 0 and JournalEntry.objects.count() == 0
    assert services.comparison_snapshot(assoc) == run


def test_balances_without_trial_balance_plug_to_equity(assoc):
    report = do_import_skip(assoc, skip=("trial_balance.csv", "owner_ledger.csv"))
    assert report["problems"] == []
    b = balances(assoc)
    assert b["7"] == D("780.04") and b["8"] == D("-50.00")
    assert trial_balance(as_of=AS_OF, association=assoc).difference == 0
    opening = JournalEntry.objects.get(source_type=engine.OPENING)
    assert opening.date == AS_OF


def do_import_skip(assoc, skip):
    run = new_run(assoc, skip=skip)
    return engine.commit(run)


def test_ownership_transfer_in_later_snapshot(assoc, tmp_path):
    do_import(assoc)
    for p in BASE.glob("*.csv"):
        text = p.read_text()
        if p.name == "owner_directory.csv":
            lines = text.splitlines()
            out = []
            for line in lines:
                if line.startswith("3,"):
                    out.append(line.replace("01/01/2020,,", "01/01/2020,09/30/2026,"))
                    out.append('3,New Owner,new@example.com,,"x",10/01/2026,,No')
                else:
                    out.append(line)
            text = "\n".join(out) + "\n"
        (tmp_path / p.name).write_text(text)
    do_import(assoc, tmp_path)
    unit3 = Unit.objects.get(number="3")
    assert OwnershipPeriod.objects.filter(unit=unit3).count() == 2
    assert [p.full_name for p in current_owners(unit3, AS_OF)] == ["New Owner"]
