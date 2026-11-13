"""Seed the HOA chart-of-accounts template for associations that already exist.

New associations get it from ``manage.py load_chart_of_accounts`` (or the seed command).
"""

from django.db import migrations


def seed(apps, schema_editor):
    from homeoa.ledger.chart import ACCOUNTS, FUNDS

    Association = apps.get_model("core", "Association")
    Fund = apps.get_model("ledger", "Fund")
    Account = apps.get_model("ledger", "Account")
    for assoc in Association.objects.all():
        for order, (code, name, is_reserve) in enumerate(FUNDS):
            Fund.objects.get_or_create(
                association=assoc,
                code=code,
                defaults={"name": name, "is_reserve": is_reserve, "sort_order": order},
            )
        for code, name, type_, receivable in ACCOUNTS:
            Account.objects.get_or_create(
                association=assoc,
                code=code,
                defaults={"name": name, "type": str(type_), "is_receivable": receivable and code == "1100"},
            )


class Migration(migrations.Migration):
    dependencies = [("ledger", "0002_ledger_triggers"), ("core", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
