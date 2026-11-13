"""Re-encrypt every encrypted field value under the newest key in HOMEOA_FIELD_KEYS."""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from homeoa.core.crypto import encrypted_fields, rotate_token


class Command(BaseCommand):
    help = "Re-encrypt all encrypted model fields with the first key in HOMEOA_FIELD_KEYS."

    def handle(self, *args, **options):
        total = 0
        qn = connection.ops.quote_name
        with transaction.atomic(), connection.cursor() as cursor:
            for model, field in encrypted_fields():
                table = qn(model._meta.db_table)
                col = qn(field.column)
                pk = qn(model._meta.pk.column)
                cursor.execute(f"SELECT {pk}, {col} FROM {table} WHERE {col} IS NOT NULL")  # noqa: S608
                rows = cursor.fetchall()
                for pk_value, token in rows:
                    cursor.execute(
                        f"UPDATE {table} SET {col} = %s WHERE {pk} = %s",  # noqa: S608
                        [rotate_token(token), pk_value],
                    )
                total += len(rows)
                self.stdout.write(f"{model._meta.label}.{field.name}: {len(rows)}")
        self.stdout.write(self.style.SUCCESS(f"Rotated {total} values."))
