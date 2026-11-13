"""Turn the unit-subledger tags into PROTECT foreign keys into the registry (M1-11).

The columns keep their names (``unit_id``, ``ownership_period_id``) and type (bigint); only
the FK constraints and an index on ``ownership_period_id`` are added to the database.
"""

import django.db.models.deletion
from django.db import migrations, models

FORWARD = """
ALTER TABLE ledger_journalline
    ADD CONSTRAINT ledger_journalline_unit_id_fk_registry_unit
    FOREIGN KEY (unit_id) REFERENCES registry_unit (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE ledger_journalline
    ADD CONSTRAINT ledger_journalline_ownership_period_id_fk_registry_period
    FOREIGN KEY (ownership_period_id) REFERENCES registry_ownershipperiod (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX ledger_journalline_ownership_period_id_idx
    ON ledger_journalline (ownership_period_id);
"""

BACKWARD = """
DROP INDEX IF EXISTS ledger_journalline_ownership_period_id_idx;
ALTER TABLE ledger_journalline
    DROP CONSTRAINT IF EXISTS ledger_journalline_ownership_period_id_fk_registry_period;
ALTER TABLE ledger_journalline
    DROP CONSTRAINT IF EXISTS ledger_journalline_unit_id_fk_registry_unit;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0004_seal_and_subledger"),
        ("registry", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(FORWARD, BACKWARD)],
            state_operations=[
                migrations.AlterField(
                    model_name="journalline",
                    name="unit_id",
                    field=models.BigIntegerField(blank=True, null=True, verbose_name="unit"),
                ),
                migrations.RenameField("journalline", "unit_id", "unit"),
                migrations.AlterField(
                    model_name="journalline",
                    name="unit",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="registry.unit",
                        verbose_name="unit",
                    ),
                ),
                migrations.RenameField(
                    "journalline", "ownership_period_id", "ownership_period"
                ),
                migrations.AlterField(
                    model_name="journalline",
                    name="ownership_period",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="registry.ownershipperiod",
                        verbose_name="ownership period",
                    ),
                ),
            ],
        ),
    ]
