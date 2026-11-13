"""Extra data on top of seed_demo, run through `manage.py shell`."""

import json

import pytest

from conftest import FIXED_TODAY, manage


def run_script(script: str, what: str) -> dict:
    r = manage("shell", "-c", script, check=False)
    if r.returncode:
        pytest.fail(f"{what} bootstrap failed: {r.stderr[-1500:]}")
    line = [x for x in r.stdout.splitlines() if x.startswith("E2EJSON")][-1]
    return json.loads(line[len("E2EJSON") :])


# The server stamps a bill's approval with the wall clock, while the demo's "today" is pinned
# to FIXED_TODAY. A bank debit only pays a bill approved on or before the debit's date, so
# scripts that add a debit dated in the demo's month first move approvals to the pinned day.
APPROVE_ON_DEMO_TODAY = f"""
import datetime as dt
from homeoa.ap.models import Bill
Bill.all_associations.filter(approved_at__isnull=False).update(
    approved_at=dt.datetime.fromisoformat("{FIXED_TODAY}T12:00:00+00:00"))
"""


# seed_demo seeds units, the schedule and Aug-Sep; post October for run pages.
BILLING = """
import datetime as dt, json
from homeoa.billing.models import AssessmentSchedule
from homeoa.billing.services import generate_assessments
from homeoa.core.context import association_context
from homeoa.core.models import Association
a = Association.objects.order_by("pk").first()
with association_context(a):
    s = AssessmentSchedule.objects.get()
    run = generate_assessments(s, dt.date(2026, 10, 1)).run
    print("E2EJSON" + json.dumps({"schedule": s.pk, "run": run.pk}))
"""


def billing():
    """On top of seed_demo: post October 2026 dues ($3,432.36)."""
    return run_script(BILLING, "billing")


BANK = """
import json
from pathlib import Path
from homeoa.banking import services
from homeoa.banking.models import BankAccount
from homeoa.core.context import association_context
from homeoa.core.models import Association
from homeoa.ledger.chart import load_chart_of_accounts
from homeoa.ledger.models import Account, Fund
a = Association.objects.order_by("pk").first()
with association_context(a):
    if not Account.objects.filter(code="1000").exists():
        load_chart_of_accounts(a)
    acct = BankAccount.objects.filter(name="Operating checking").first()
    if acct is None:
        acct = BankAccount.objects.create(
            association=a, name="Operating checking", account_number="000123454321",
            gl_account=Account.objects.get(code="1000"), fund=Fund.objects.get(code="operating"))
    batch = acct.batches.order_by("-pk").first()
    if batch is None:
        data = Path(__CSV__).read_bytes()
        services.import_file(acct, "bank_oct.csv", data)
        batch = acct.batches.order_by("-pk").first()
    txn = batch.transactions.order_by("pk").first()
    print("E2EJSON" + json.dumps({"acct": acct.pk, "batch": batch.pk, "txn": txn and txn.pk}))
"""


def bank():
    """One bank account ("Operating checking") with bank_oct.csv imported once."""
    from pathlib import Path

    csv = Path(__file__).resolve().parents[1] / "fixtures" / "bank_oct.csv"
    return run_script(BANK.replace("__CSV__", repr(str(csv))), "bank")
