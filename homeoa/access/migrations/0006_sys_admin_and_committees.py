"""Instance admin becomes System admin, the finance backup role goes away (the Treasurer's
fallback is now a change request), and the Maintenance committee role is added."""

from django.db import migrations


def forward(apps, schema_editor):
    Association = apps.get_model("core", "Association")
    Role = apps.get_model("access", "Role")
    RoleAssignment = apps.get_model("access", "RoleAssignment")
    Role.objects.filter(slug="instance_admin").update(slug="sys_admin", name="System admin")
    backup = Role.objects.filter(slug="finance_backup")
    RoleAssignment.objects.filter(role__in=backup).delete()
    backup.delete()
    for association in Association.objects.all():
        Role.objects.get_or_create(
            association=association,
            slug="maintenance_committee",
            defaults={"name": "Maintenance committee", "is_system": True},
        )


class Migration(migrations.Migration):
    dependencies = [("access", "0005_role_member_limit"), ("core", "0001_initial")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
