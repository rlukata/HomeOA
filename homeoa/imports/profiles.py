"""Built-in mapping profiles. Every value is only a default: the mapper UI edits all of it.

The eCondo profile matches an assumed AppFolio-style export layout (the synthetic files in
e2e/fixtures/econdo/ follow it). Real exports may differ; the mapper UI adjusts the columns.

Config shape (stored on ImportRun.config and MappingProfile.config)::

    {
      "date_format": "%m/%d/%Y",
      "files": {"unit_directory.csv": "units", ...},        # filename -> kind
      "columns": {"units": {"number": "Unit", ...}, ...},   # kind -> field -> header
      "accounts": {"1200": {"account": "1100", "fund": "operating"}, ...},
      "account_names": {"insurance expense": {"account": "5300", "fund": "insurance"}},
      "history_types": {"Charge": "4000", "Payment": "1000", ...},  # counter account
      "opening_equity": "3000",
    }
"""

from __future__ import annotations

import copy

ECONDO = "econdo"
GENERIC = "csv"

NEW_ACCOUNT = "__new__"

ECONDO_PROFILE = {
    "date_format": "%m/%d/%Y",
    "files": {
        "unit_directory.csv": "units",
        "owner_directory.csv": "owners",
        "tenant_directory.csv": "tenants",
        "owner_ledger.csv": "history",
        "owner_balances.csv": "balances",
        "chart_of_accounts.csv": "accounts",
        "trial_balance.csv": "trial_balance",
    },
    "columns": {
        "units": {
            "number": "Unit",
            "building": "Building",
            "address": "Address",
            "parcel_id": "Parcel ID",
            "allocated_interest": "Allocated Interest %",
            "status": "Status",
        },
        "owners": {
            "unit": "Unit",
            "name": "Owner Name",
            "email": "Primary Email",
            "phone": "Phone",
            "mailing_address": "Mailing Address",
            "start": "Move In",
            "end": "Move Out",
            "e_delivery": "E-Delivery",
        },
        "tenants": {
            "unit": "Unit",
            "name": "Tenant Name",
            "email": "Email",
            "phone": "Phone",
            "start": "Lease Start",
            "end": "Lease End",
        },
        "history": {
            "unit": "Unit",
            "date": "Date",
            "type": "Type",
            "description": "Description",
            "charge": "Charge",
            "payment": "Payment",
            "balance": "Balance",
            "reference": "Reference",
        },
        "balances": {
            "unit": "Unit",
            "name": "Owner Name",
            "balance": "Balance",
            "prepaid": "Prepaid",
        },
        "accounts": {
            "code": "Account Number",
            "name": "Account Name",
            "type": "Account Type",
            "fund": "Fund",
        },
        "trial_balance": {
            "code": "Account Number",
            "name": "Account Name",
            "debit": "Debit",
            "credit": "Credit",
        },
    },
    # eCondo account -> HomeOA account where the codes differ. Same codes map to themselves.
    "accounts": {
        "1200": {"account": "1100"},
        "3000": {"account": "3000"},
        "4900": {"account": "4200"},
    },
    # eCondo account name (case-insensitive) -> HomeOA account and fund, used when the code
    # has no entry in "accounts". Insurance fund lines, from the 2026 budget's wording.
    "account_names": {
        "transfer to insurance reserves": {"account": "5910", "fund": "operating"},
        "insurance assessments": {"account": "4910", "fund": "insurance"},
        "insurance expense": {"account": "5300", "fund": "insurance"},
    },
    "history_types": {
        "Charge": "4000",
        "Late Fee": "4100",
        "Payment": "1000",
        "Credit": "4000",
        "Adjustment": "4200",
    },
    "opening_equity": "3000",
}

GENERIC_PROFILE = {
    "date_format": "%m/%d/%Y",
    "files": {},
    "columns": {},
    "accounts": {},
    "history_types": copy.deepcopy(ECONDO_PROFILE["history_types"]),
    "opening_equity": "3000",
}


def account_choice(config: dict, code: str, name: str | None, source: str = ECONDO) -> dict:
    """The mapping for one external account: by code first, then by name. See ``ECONDO_PROFILE``.

    Runs saved before ``account_names`` existed fall back to the source's built-in names.
    """
    choice = (config.get("accounts") or {}).get(code)
    if choice:
        return choice
    names = config.get("account_names")
    if names is None:
        names = default_config(source).get("account_names", {})
    return names.get((name or "").strip().lower(), {})


def default_config(source: str) -> dict:
    return copy.deepcopy(ECONDO_PROFILE if source == ECONDO else GENERIC_PROFILE)
