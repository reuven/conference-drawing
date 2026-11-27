"""Key every entrant that existed before `email_key` did.

Data only, in chunks so a large install never loads every row at once. It
imports `email_key` from `raffle.text`, which is pure (no model imports), so
the migration and the entry form cannot disagree on who is the same person.
Nothing is merged or deleted: an install that already holds `ada@x` and
`ada+1@x` keeps both rows. The reverse is a no-op; the column just stays.
"""

from django.db import migrations
from django.db.migrations.state import StateApps
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from conference_drawing.raffle.text import email_key

_CHUNK = 500


def backfill(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Set `email_key` on every entrant that has none."""
    entrant = apps.get_model("raffle", "Entrant")
    pending = entrant.objects.filter(email_key__isnull=True).only("id", "email")
    batch = []
    for row in pending.iterator(chunk_size=_CHUNK):
        row.email_key = email_key(row.email)
        batch.append(row)
        if len(batch) == _CHUNK:
            entrant.objects.bulk_update(batch, ["email_key"])
            batch = []
    if batch:
        entrant.objects.bulk_update(batch, ["email_key"])


class Migration(migrations.Migration):

    dependencies = [
        ("raffle", "0013_entrant_email_key"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
