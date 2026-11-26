"""Rename migrations must be state-only so v0.2.1 keeps running on the new schema."""

import io

import pytest
from django.core.management import call_command

_IGNORED = {"BEGIN;", "COMMIT;"}


@pytest.mark.parametrize("name", ["0010_reserved_slugs", "0011_newsletter_names"])
def test_rename_migration_emits_no_sql(db: None, name: str) -> None:
    """A state-only migration touches no column, so rollback to v0.2.1 is safe."""
    out = io.StringIO()
    call_command("sqlmigrate", "raffle", name, stdout=out)
    statements = [
        line
        for line in out.getvalue().splitlines()
        if line.strip() and line.strip() not in _IGNORED and not line.startswith("--")
    ]
    assert statements == []
