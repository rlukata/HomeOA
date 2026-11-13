"""Postgres trigger: signature events are append-only. UPDATE and DELETE always fail."""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION esign_signatureevent_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'esign_signatureevent is append-only (% rejected)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER esign_signatureevent_no_update_delete
    BEFORE UPDATE OR DELETE ON esign_signatureevent
    FOR EACH ROW EXECUTE FUNCTION esign_signatureevent_immutable();
"""

REVERSE = """
DROP TRIGGER IF EXISTS esign_signatureevent_no_update_delete ON esign_signatureevent;
DROP FUNCTION IF EXISTS esign_signatureevent_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("esign", "0003_reviewed_event")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
