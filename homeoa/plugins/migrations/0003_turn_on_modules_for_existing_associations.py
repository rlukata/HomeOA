"""Associations that existed before module switches keep every optional module on."""

from django.db import migrations

MODULE_IDS = (
    "violations",
    "arc",
    "owner_requests",
    "work_orders",
    "announcements",
    "calendar",
    "polls",
    "directory",
    "committees",
    "online_payments",
    "board_packet",
)


def turn_on_every_module(apps, schema_editor):
    Association = apps.get_model("core", "Association")
    ModuleSwitch = apps.get_model("homeoa_plugins", "ModuleSwitch")
    for association in Association.objects.all():
        for module_id in MODULE_IDS:
            # On since the association began, so module jobs still catch up missed days.
            ModuleSwitch.objects.get_or_create(
                association=association,
                module_id=module_id,
                defaults={"enabled": True, "changed_at": association.created_at},
            )


def remove_module_switches(apps, schema_editor):
    ModuleSwitch = apps.get_model("homeoa_plugins", "ModuleSwitch")
    ModuleSwitch.objects.filter(module_id__in=MODULE_IDS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("homeoa_plugins", "0002_module_switch"),
    ]

    operations = [
        migrations.RunPython(turn_on_every_module, remove_module_switches),
    ]
