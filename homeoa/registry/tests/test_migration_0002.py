"""registry 0002: lease files move from the FileField onto the FileStorage provider."""

import datetime

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from homeoa import providers

BEFORE = [("registry", "0001_initial")]
AFTER = [("registry", "0002_lease_filestorage")]


def _migrate(targets=None):
    executor = MigrationExecutor(connection)
    targets = targets or executor.loader.graph.leaf_nodes()
    executor.migrate(targets)
    return MigrationExecutor(connection).loader.project_state(targets).apps


@pytest.mark.django_db(transaction=True)
def test_copies_existing_lease_and_reports_missing(settings, tmp_path, caplog, capsys):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.HOMEOA_FILE_ROOT = str(tmp_path / "files")
    try:
        old = _migrate(BEFORE)
        Association = old.get_model("core", "Association")
        Unit = old.get_model("registry", "Unit")
        Person = old.get_model("registry", "Person")
        Occupancy = old.get_model("registry", "Occupancy")
        assoc = Association.objects.create(name="Mig HOA", short_name="MH")
        unit = Unit.objects.create(association=assoc, number="1")
        person = Person.objects.create(association=assoc, full_name="Tenant")
        saved = default_storage.save("registry/leases/2026/lease.pdf", ContentFile(b"%PDF-ok"))
        ok = Occupancy.objects.create(
            association=assoc,
            unit=unit,
            person=person,
            start=datetime.date(2026, 1, 1),
            lease_document=saved,
        )
        gone = Occupancy.objects.create(
            association=assoc,
            unit=unit,
            person=person,
            start=datetime.date(2025, 1, 1),
            lease_document="registry/leases/2025/missing.pdf",
        )

        new = _migrate(AFTER)
        Occ = new.get_model("registry", "Occupancy")
        ok, gone = Occ.objects.get(pk=ok.pk), Occ.objects.get(pk=gone.pk)
        assert ok.lease_key
        assert providers.get_provider("FileStorage").get(ok.lease_key) == b"%PDF-ok"
        assert gone.lease_key == ""
        expected = f"Occupancy {gone.pk}: registry/leases/2025/missing.pdf"
        assert expected in caplog.text
        assert expected in capsys.readouterr().out
    finally:
        _migrate()
