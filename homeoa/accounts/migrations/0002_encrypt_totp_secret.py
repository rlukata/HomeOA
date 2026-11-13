"""Encrypt ``TOTPDevice.secret`` at rest: widen the column, then encrypt existing rows."""

from cryptography.fernet import InvalidToken
from django.db import migrations

import homeoa.core.crypto


def _rows(schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id, secret FROM accounts_totpdevice")
        return cur.fetchall()


def _is_token(value: str) -> bool:
    try:
        homeoa.core.crypto.get_fernet().decrypt(value.encode())
    except InvalidToken:
        return False
    return True


def encrypt_existing(apps, schema_editor):
    rows = _rows(schema_editor)
    with schema_editor.connection.cursor() as cur:
        for pk, secret in rows:
            if secret is None or _is_token(secret):
                continue
            cur.execute(
                "UPDATE accounts_totpdevice SET secret = %s WHERE id = %s",
                [homeoa.core.crypto.encrypt(secret), pk],
            )


def decrypt_existing(apps, schema_editor):
    rows = _rows(schema_editor)
    with schema_editor.connection.cursor() as cur:
        for pk, secret in rows:
            if secret is None or not _is_token(secret):
                continue
            cur.execute(
                "UPDATE accounts_totpdevice SET secret = %s WHERE id = %s",
                [homeoa.core.crypto.decrypt(secret), pk],
            )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="totpdevice",
            name="secret",
            field=homeoa.core.crypto.EncryptedTextField(),
        ),
        migrations.RunPython(encrypt_existing, decrypt_existing),
    ]
