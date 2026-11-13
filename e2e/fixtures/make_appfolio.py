"""Regenerate e2e/fixtures/appfolio/{base,later_unit7_latefee}/ from the Cedar Row
sample, in the layout homeoa/imports/profiles.py expects.
Run: python e2e/fixtures/make_appfolio.py"""

import csv
from decimal import Decimal as D
from pathlib import Path

OUT = Path(__file__).parent / "appfolio"
ADDR = "14207 Cedar Row Ln #{u}, Olympia, WA 98501"
UNITS = [  # unit, bldg, owner, share, dues, occupancy, tenant
    ("A", "", "Maya Nguyen", "11.5556", "396.63", None),
    ("B", "", "James Okafor", "11.5556", "396.63", None),
    ("C", "", "Priya Raman", "11.5556", "396.63", None),
    ("D", "", "Daniel Kim", "10.6667", "366.12", None),
    ("E", "", "Sofia Herrera", "11.5556", "396.63", "Chris Doyle"),
    ("F", "", "Tom & Lisa Becker", "11.1111", "381.37", "Sam Rivera"),
    ("G", "", "Abdi Warsame", "10.2221", "350.86", None),
    ("H", "", "Elena Petrova", "10.2221", "350.86", "Jordan Ellis"),
    ("I", "", "Rafael Ortiz", "11.5556", "396.63", None),
]
UNPAID_OCT = {"C", "G", "I"}


def money(x: D) -> str:
    return f"({-x:,.2f})" if x < 0 else f"{x:,.2f}"


def email(name: str) -> str:
    return name.split(" & ")[0].lower().replace(" ", ".") + "@example.com"


def write(folder: Path, name: str, header: list[str], rows: list[list]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def ledger_rows(unit: str, dues: D, extra_fee: bool) -> list[list]:
    rows, bal = [], D(0)

    def add(date, typ, desc, charge=None, payment=None, ref=""):
        nonlocal bal
        bal += (charge or 0) - (payment or 0)
        rows.append(
            [
                unit,
                date,
                typ,
                desc,
                money(charge) if charge else "",
                money(payment) if payment else "",
                money(bal),
                ref,
            ]
        )

    for mm, mon in (("08", "August"), ("09", "September"), ("10", "October")):
        add(f"{mm}/01/2026", "Charge", f"Monthly dues · {mon}", charge=dues)
        if unit == "G" and mm == "09":
            add("09/11/2026", "Late Fee", "Late fee", charge=D("25.00"))
            continue
        if mm == "10" and unit in UNPAID_OCT:
            continue
        add(
            f"{mm}/05/2026",
            "Payment",
            "Payment",
            payment=dues + (D("50.00") if unit == "H" and mm == "10" else 0),
            ref=f"{unit}{mm}",
        )
    if unit == "G" and extra_fee:
        add("10/11/2026", "Late Fee", "Late fee", charge=D("25.00"))
    return rows


def build(name: str, extra_fee: bool) -> None:
    folder = OUT / name
    write(
        folder,
        "unit_directory.csv",
        ["Unit", "Building", "Address", "Parcel ID", "Allocated Interest %", "Status"],
        [
            [u, b, ADDR.format(u=u), "", s, "Tenant Occupied" if t else "Owner Occupied"]
            for u, b, _, s, _, t in UNITS
        ],
    )
    owners = []
    for u, _, o, *_ in UNITS:
        names = (
            [o]
            if " & " not in o
            else [n + " Becker" if "Becker" not in n else n for n in o.split(" & ")]
        )
        for n in names:
            owners.append([u, n, email(n), "", ADDR.format(u=u), "01/01/2020", "", "Yes"])
    write(
        folder,
        "owner_directory.csv",
        [
            "Unit",
            "Owner Name",
            "Primary Email",
            "Phone",
            "Mailing Address",
            "Move In",
            "Move Out",
            "E-Delivery",
        ],
        owners,
    )
    write(
        folder,
        "tenant_directory.csv",
        ["Unit", "Tenant Name", "Email", "Phone", "Lease Start", "Lease End"],
        [[u, t, email(t), "", "01/01/2026", "12/31/2026"] for u, *_, t in UNITS if t],
    )
    ledger, balances = [], []
    for u, _, o, _, dues, _ in UNITS:
        rows = ledger_rows(u, D(dues), extra_fee)
        ledger += rows
        bal = D(rows[-1][6].replace(",", "").replace("(", "-").replace(")", ""))
        cur = max(bal, D(0))
        balances.append(
            [
                u,
                o,
                money(bal),
                money(cur if u != "G" else D(dues) + (25 if extra_fee else 0)),
                money(D(0) if u != "G" else bal - D(dues) - (25 if extra_fee else 0)),
                "0.00",
                "0.00",
                money(-bal) if bal < 0 else "0.00",
            ]
        )
    write(
        folder,
        "owner_ledger.csv",
        ["Unit", "Date", "Type", "Description", "Charge", "Payment", "Balance", "Reference"],
        ledger,
    )
    write(
        folder,
        "owner_balances.csv",
        ["Unit", "Owner Name", "Balance", "0-30", "31-60", "61-90", "90+", "Prepaid"],
        balances,
    )
    coa = [
        ["1000", "Operating Cash", "Asset", "Operating"],
        ["1010", "Reserve Cash", "Asset", "Reserve"],
        ["1200", "Assessments Receivable", "Asset", "Operating"],
        ["2100", "Prepaid Assessments", "Liability", "Operating"],
        ["3000", "Opening Balance Equity", "Equity", "Operating"],
        ["4000", "Assessment Income", "Income", "Operating"],
        ["4100", "Late Fee Income", "Income", "Operating"],
        ["4900", "Other Income", "Income", "Operating"],
    ]
    write(
        folder,
        "chart_of_accounts.csv",
        ["Account Number", "Account Name", "Account Type", "Fund"],
        coa,
    )
    fees = D("50.00") if extra_fee else D("25.00")
    ar = sum(D(b[2].replace(",", "")) for b in balances if not b[2].startswith("("))
    debits = D("14382.17") + D("41950.00") + ar
    credits_ex_equity = D("50.00") + D("10297.08") + fees
    equity = debits - credits_ex_equity  # same in both snapshots
    tb = [
        ["1000", "Operating Cash", money(D("14382.17")), ""],
        ["1010", "Reserve Cash", money(D("41950.00")), ""],
        ["1200", "Assessments Receivable", money(ar), ""],
        ["2100", "Prepaid Assessments", "", "50.00"],
        ["3000", "Opening Balance Equity", "", money(equity)],
        ["4000", "Assessment Income", "", "10,297.08"],
        ["4100", "Late Fee Income", "", money(fees)],
    ]
    assert debits == D("50.00") + equity + D("10297.08") + fees, "trial balance must balance"
    write(folder, "trial_balance.csv", ["Account Number", "Account Name", "Debit", "Credit"], tb)


if __name__ == "__main__":
    build("base", extra_fee=False)
    build("later_unit7_latefee", extra_fee=True)
