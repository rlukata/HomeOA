"""Move Occupancy.lease_document (Django FileField) onto the FileStorage provider.

Each existing file is read from Django's default storage, written through the association's
FileStorage provider, and its key stored in ``lease_key``. Missing files are skipped (the key
stays blank) rather than failing the migration; each skip is logged and printed with the
Occupancy id and file name so the operator can re-upload it.
"""

import logging
import mimetypes

from django.core.files.storage import default_storage
from django.db import migrations, models

log = logging.getLogger("homeoa.registry.migrations")


def copy_leases(apps, schema_editor):
    from homeoa import providers

    Occupancy = apps.get_model("registry", "Occupancy")
    for occ in Occupancy.objects.exclude(lease_document="").select_related("association"):
        name = occ.lease_document.name
        try:
            with default_storage.open(name, "rb") as fh:
                data = fh.read()
        except OSError:
            msg = f"lease file missing for Occupancy {occ.pk}: {name} (lease_key left blank)"
            log.warning(msg)
            print(f"  {msg}")
            continue
        storage = providers.get_provider("FileStorage", occ.association)
        filename = name.rsplit("/", 1)[-1]
        stored = storage.put(
            occ.association_id,
            filename,
            data,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            folder="registry/leases",
        )
        occ.lease_key = stored.key
        occ.save(update_fields=["lease_key"])


class Migration(migrations.Migration):
    dependencies = [
        ("registry", "0001_initial"),
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="occupancy",
            name="lease_key",
            field=models.CharField(blank=True, max_length=300, verbose_name="lease"),
        ),
        migrations.RunPython(copy_leases, migrations.RunPython.noop),
        migrations.RemoveField(model_name="occupancy", name="lease_document"),
    ]
