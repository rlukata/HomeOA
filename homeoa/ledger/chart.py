"""HOA chart-of-accounts template (data) and the loader that seeds it per association.

The template is generic HOA bookkeeping, not jurisdiction rules. Boards can rename, add or
deactivate accounts afterwards; loading is idempotent and never overwrites existing codes.

Funds: Operating, Reserve and Insurance. Owners are billed in operating
only; reserve and insurance money moves by monthly interfund entries.

Interfund accounts. Each pair of funds has its own due-from (asset) / due-to (liability) pair
so the balances of one relationship never net against another:

* operating <-> reserve: 1900 Due from other fund / 2900 Due to other fund.
* any pair involving insurance: 1920 Due from other fund (insurance) / 2920 Due to
  other fund (insurance). The monthly transfer posts operating Dr 5910 / Cr 2920 and
  insurance Dr 1920 / Cr 4910.

5300 "Insurance" is the premium expense; premiums post in the insurance fund.
1020 Cash – Insurance and 3200 Fund balance – Insurance complete the fund.
"""

from __future__ import annotations

from django.utils.translation import gettext_noop as N_

from .models import Account, AccountType, Fund

# (code, name, is_reserve)
FUNDS = (
    ("operating", N_("Operating"), False),
    ("reserve", N_("Reserve"), True),
    ("insurance", N_("Insurance"), False),
)

INSURANCE_FUND = "insurance"

# Fund code -> fund balance (equity) account code, from the template below.
FUND_BALANCE_CODES = {"operating": "3000", "reserve": "3100", "insurance": "3200"}

# source_type of year-end closing entries (closing app). Carried over on the cash view
# (reports.queries.CARRY_OVER) and excluded from statement activity.
CLOSE_SOURCE = "ledger.close"

A, L, E, INC, X = (
    AccountType.ASSET,
    AccountType.LIABILITY,
    AccountType.EQUITY,
    AccountType.INCOME,
    AccountType.EXPENSE,
)

# (code, name, type, in_unit_subledger). Names are stored as data in the board's language.
ACCOUNTS = (
    ("1000", N_("Cash – Operating"), A, False),
    ("1010", N_("Cash – Reserve"), A, False),
    ("1020", N_("Cash – Insurance"), A, False),
    ("1060", N_("Stripe clearing"), A, False),
    ("1100", N_("Assessments receivable"), A, True),
    ("1900", N_("Due from other fund"), A, False),
    ("1920", N_("Due from other fund (insurance)"), A, False),
    ("2000", N_("Accounts payable"), L, False),
    ("2100", N_("Prepaid assessments"), L, True),
    ("2900", N_("Due to other fund"), L, False),
    ("2920", N_("Due to other fund (insurance)"), L, False),
    ("3000", N_("Fund balance – Operating"), E, False),
    ("3100", N_("Fund balance – Reserve"), E, False),
    ("3200", N_("Fund balance – Insurance"), E, False),
    ("4000", N_("Assessment income"), INC, False),
    ("4050", N_("Special assessment income"), INC, False),
    ("4100", N_("Late fee income"), INC, False),
    ("4150", N_("Fine income"), INC, False),
    ("4200", N_("Other income"), INC, False),
    ("4300", N_("Interest income"), INC, False),
    ("4900", N_("Reserve contributions received"), INC, False),
    ("4910", N_("Insurance transfer income"), INC, False),
    ("5000", N_("Utilities"), X, False),
    ("5100", N_("Landscaping"), X, False),
    ("5200", N_("Repairs & maintenance"), X, False),
    ("5300", N_("Insurance"), X, False),
    ("5400", N_("Management & administration"), X, False),
    ("5450", N_("Bank & payment processing fees"), X, False),
    ("5800", N_("Bad debt expense"), X, False),
    ("5900", N_("Reserve contributions"), X, False),
    ("5910", N_("Transfer to insurance"), X, False),
)


def _fund_code(fund) -> str:
    return fund if isinstance(fund, str) else fund.code


def interfund_codes(fund_a, fund_b) -> tuple[str, str]:
    """(due_from, due_to) account codes for money between two funds. See module doc."""
    if INSURANCE_FUND in {_fund_code(fund_a), _fund_code(fund_b)}:
        return "1920", "2920"
    return "1900", "2900"


def load_chart_of_accounts(association) -> tuple[int, int]:
    """Create any missing template funds and accounts. Returns (funds_created, accounts_created)."""
    funds = 0
    for order, (code, name, is_reserve) in enumerate(FUNDS):
        _obj, created = Fund.all_associations.get_or_create(
            association=association,
            code=code,
            defaults={"name": name, "is_reserve": is_reserve, "sort_order": order},
        )
        funds += created
    accounts = 0
    for code, name, type_, receivable in ACCOUNTS:
        _obj, created = Account.all_associations.get_or_create(
            association=association,
            code=code,
            defaults={"name": name, "type": type_, "in_unit_subledger": receivable},
        )
        accounts += created
    return funds, accounts
