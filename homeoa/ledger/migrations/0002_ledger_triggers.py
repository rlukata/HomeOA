"""Postgres guards for the ledger.

* Journal entries and lines are immutable: UPDATE and DELETE always fail.
* A deferred constraint trigger checks at commit that every entry touched balances per fund
  (debits = credits within each fund, hence in total), backing the service assertion.
"""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION ledger_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is immutable once posted (% rejected); post a reversing entry',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_journalentry_immutable
    BEFORE UPDATE OR DELETE ON ledger_journalentry
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();
CREATE TRIGGER ledger_journalline_immutable
    BEFORE UPDATE OR DELETE ON ledger_journalline
    FOR EACH ROW EXECUTE FUNCTION ledger_immutable();

CREATE OR REPLACE FUNCTION ledger_check_entry_balanced() RETURNS trigger AS $$
DECLARE
    bad_fund bigint;
BEGIN
    SELECT fund_id INTO bad_fund
      FROM ledger_journalline
     WHERE entry_id = NEW.entry_id
     GROUP BY fund_id
    HAVING SUM(debit) <> SUM(credit)
     LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'journal entry % does not balance in fund %', NEW.entry_id, bad_fund
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER ledger_journalline_balanced
    AFTER INSERT ON ledger_journalline
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_entry_balanced();
"""

REVERSE = """
DROP TRIGGER IF EXISTS ledger_journalline_balanced ON ledger_journalline;
DROP FUNCTION IF EXISTS ledger_check_entry_balanced();
DROP TRIGGER IF EXISTS ledger_journalline_immutable ON ledger_journalline;
DROP TRIGGER IF EXISTS ledger_journalentry_immutable ON ledger_journalentry;
DROP FUNCTION IF EXISTS ledger_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [("ledger", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
