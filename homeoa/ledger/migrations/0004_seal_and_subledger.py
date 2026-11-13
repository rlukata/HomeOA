"""Seal journal entries, and rename the unit-subledger flag (adds 2100 Prepaid assessments).

Sealing closes the INSERT gap: once ``post_entry`` sets ``sealed_at`` no more lines can be
added to that entry, and an entry still unsealed at commit is rejected.
"""

from django.db import migrations, models

SQL = """
DROP TRIGGER IF EXISTS ledger_journalentry_immutable ON ledger_journalentry;

UPDATE ledger_journalentry SET sealed_at = posted_at WHERE sealed_at IS NULL;

CREATE OR REPLACE FUNCTION ledger_journalentry_guard() RETURNS trigger AS $$
DECLARE
    probe ledger_journalentry;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL THEN
        probe := NEW;
        probe.sealed_at := NULL;
        IF probe IS NOT DISTINCT FROM OLD THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'ledger_journalentry is immutable once posted (% rejected); post a reversing entry',
        TG_OP USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_journalentry_immutable
    BEFORE UPDATE OR DELETE ON ledger_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_journalentry_guard();

CREATE OR REPLACE FUNCTION ledger_line_entry_open() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM ledger_journalentry
                WHERE id = NEW.entry_id AND sealed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'journal entry % is sealed; lines cannot be added', NEW.entry_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_journalline_entry_open
    BEFORE INSERT ON ledger_journalline
    FOR EACH ROW EXECUTE FUNCTION ledger_line_entry_open();

CREATE OR REPLACE FUNCTION ledger_check_entry_sealed() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM ledger_journalentry WHERE id = NEW.id AND sealed_at IS NULL) THEN
        RAISE EXCEPTION 'journal entry % was not sealed by the posting service', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER ledger_journalentry_sealed
    AFTER INSERT ON ledger_journalentry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_sealed();
"""

REVERSE = """
DROP TRIGGER IF EXISTS ledger_journalentry_sealed ON ledger_journalentry;
DROP FUNCTION IF EXISTS ledger_check_entry_sealed();
DROP TRIGGER IF EXISTS ledger_journalline_entry_open ON ledger_journalline;
DROP FUNCTION IF EXISTS ledger_line_entry_open();
DROP TRIGGER IF EXISTS ledger_journalentry_immutable ON ledger_journalentry;
DROP FUNCTION IF EXISTS ledger_journalentry_guard();
CREATE TRIGGER ledger_journalentry_immutable
    BEFORE UPDATE OR DELETE ON ledger_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
"""


def flag_prepaid(apps, schema_editor):
    Account = apps.get_model("ledger", "Account")
    Account.objects.filter(code__in=["1100", "2100"]).update(in_unit_subledger=True)


class Migration(migrations.Migration):
    dependencies = [("ledger", "0003_seed_chart_of_accounts")]

    operations = [
        migrations.RenameField("account", "is_receivable", "in_unit_subledger"),
        migrations.AlterField(
            model_name="account",
            name="in_unit_subledger",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Lines on this account make up each unit's balance (receivable and prepaid)."
                ),
                verbose_name="in unit subledger",
            ),
        ),
        migrations.AddField(
            model_name="journalentry",
            name="sealed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set by the posting service once all lines are in; no lines after that.",
                null=True,
                verbose_name="sealed at",
            ),
        ),
        migrations.RunPython(flag_prepaid, migrations.RunPython.noop),
        migrations.RunSQL(SQL, REVERSE),
    ]
