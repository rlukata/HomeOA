"""Seed the default system roles for existing associations (new ones get them on save)."""

from django.db import migrations

ROLES = {
    "owner": "Owner",
    "tenant": "Tenant",
    "committee": "Committee",
    "board": "Board",
    "cpa": "CPA (read-only)",
    "instance_admin": "Instance admin",
}


def seed(apps, schema_editor):
    Association = apps.get_model("core", "Association")
    Role = apps.get_model("access", "Role")
    for association in Association.objects.all():
        for slug, name in ROLES.items():
            Role.objects.get_or_create(
                association=association, slug=slug, defaults={"name": name, "is_system": True}
            )


class Migration(migrations.Migration):
    dependencies = [("access", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
