"""D-45: add the Insurance fund and its accounts to associations whose chart is loaded."""

from django.db import migrations


def seed(apps, schema_editor):
    from homeoa.ledger.chart import ACCOUNTS, FUNDS

    Association = apps.get_model("core", "Association")
    Fund = apps.get_model("ledger", "Fund")
    Account = apps.get_model("ledger", "Account")
    for assoc in Association.objects.all():
        if not Fund.objects.filter(association=assoc).exists():
            continue  # chart never loaded; the loader adds everything when it is
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
                defaults={"name": name, "type": str(type_), "in_unit_subledger": receivable},
            )


class Migration(migrations.Migration):
    dependencies = [("ledger", "0005_subledger_foreign_keys"), ("core", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
