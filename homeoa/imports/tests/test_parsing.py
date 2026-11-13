import datetime as dt
from decimal import Decimal

import pytest

from homeoa.imports.appfolio import AppFolioImporter
from homeoa.imports.parsing import (
    KINDS,
    CellError,
    guess_columns,
    parse_bool,
    parse_date,
    parse_file,
    parse_money,
    split_address,
)

D = Decimal


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("1,234.56", D("1234.56")),
        ("$1,234.56", D("1234.56")),
        ("(25.00)", D("-25.00")),
        ("-25", D("-25.00")),
        ("25.00-", D("-25.00")),
        ("", None),
        ("  ", None),
    ],
)
def test_parse_money(raw, want):
    got = parse_money(raw)
    assert got == want
    assert got is None or isinstance(got, Decimal)


@pytest.mark.parametrize("raw", ["abc", "1.234", "1e5", "12.3.4"])
def test_parse_money_rejects(raw):
    with pytest.raises(CellError):
        parse_money(raw)


def test_parse_date_formats():
    assert parse_date("10/01/2026") == dt.date(2026, 10, 1)
    assert parse_date("2026-10-01", "%Y-%m-%d") == dt.date(2026, 10, 1)
    assert parse_date("") is None
    with pytest.raises(CellError, match="MM/DD/YYYY"):
        parse_date("2026-10-01")


def test_parse_bool():
    assert parse_bool("Yes") is True and parse_bool("no") is False and parse_bool("") is None
    with pytest.raises(CellError):
        parse_bool("maybe")


def test_split_address():
    a = split_address("14207 Cedar Row Ln #7, Olympia, WA 98501")
    assert a == {
        "address_line1": "14207 Cedar Row Ln #7",
        "city": "Olympia",
        "region": "WA",
        "postal_code": "98501",
    }
    assert split_address("PO Box 5")["address_line1"] == "PO Box 5"


def test_guess_columns_from_appfolio_headers():
    cols = guess_columns(KINDS["owners"], ["Unit", "Owner Name", "Primary Email", "Move In"])
    assert cols == {
        "unit": "Unit",
        "name": "Owner Name",
        "email": "Primary Email",
        "start": "Move In",
    }


def test_first_and_last_name_are_joined():
    problems = []
    p = parse_file(
        "tenants",
        "t.csv",
        "Unit,First,Last,Start\n5,Chris,Doyle,01/01/2026\n",
        {"unit": "Unit", "first_name": "First", "last_name": "Last", "start": "Start"},
        "%m/%d/%Y",
        problems,
    )
    assert problems == []
    assert p.rows[0]["name"] == "Chris Doyle"


def test_missing_required_column_and_bad_cell_reported():
    problems = []
    parse_file(
        "history",
        "h.csv",
        "Unit,Date\n1,x\n",
        {"unit": "Unit", "date": "Date"},
        "%m/%d/%Y",
        problems,
    )
    assert any("Type" in p.message for p in problems)
    problems = []
    parse_file(
        "history",
        "h.csv",
        "Unit,Date,Type\n1,13/45/2026,Charge\n",
        {"unit": "Unit", "date": "Date", "type": "Type"},
        "%m/%d/%Y",
        problems,
    )
    assert [(p.file, p.row, p.column) for p in problems] == [("h.csv", 2, "Date")]


def test_appfolio_importer_preview_uses_default_profile():
    out = AppFolioImporter().preview(
        "balances", b"Unit,Owner Name,Balance,Prepaid\n8,Elena,(50.00),50.00\n", {}
    )
    assert out.errors == ()
    assert out.rows[0]["balance"] == D("-50.00")
