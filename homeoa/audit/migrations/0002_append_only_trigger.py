"""Postgres trigger: audit events are append-only. UPDATE and DELETE always fail."""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION audit_auditevent_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_auditevent is append-only (% rejected)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_auditevent_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION audit_auditevent_immutable();
"""

REVERSE = """
DROP TRIGGER IF EXISTS audit_auditevent_no_update_delete ON audit_auditevent;
DROP FUNCTION IF EXISTS audit_auditevent_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
