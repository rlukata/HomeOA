from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """pg_trgm: finds misspelled names ("Jonson" finds "Johnson") for search (D-138)."""

    dependencies = []

    operations = [TrigramExtension()]
