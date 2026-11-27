"""The email_key backfill keys rows that existed before the field did."""

import importlib
from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

_BEFORE = [("raffle", "0013_entrant_email_key")]
_AFTER = [("raffle", "0014_backfill_entrant_email_key")]


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    """A migration executor that leaves the test DB fully migrated afterwards."""
    ex = MigrationExecutor(connection)
    yield ex
    ex = MigrationExecutor(connection)
    ex.migrate(ex.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("chunk", [500, 2])
def test_backfill_keys_existing_rows(
    executor: MigrationExecutor, monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    """Both the one-batch path (500) and a full-batch flush (2 of 3 rows) work."""
    monkeypatch.setattr(
        importlib.import_module(
            "conference_drawing.raffle.migrations.0014_backfill_entrant_email_key"
        ),
        "_CHUNK",
        chunk,
    )
    executor.migrate(_BEFORE)
    old = executor.loader.project_state(_BEFORE).apps
    conference = old.get_model("raffle", "Conference").objects.create(
        name="C",
        slug="c",
        time_zone="UTC",
        starts_on="2026-01-01",
        ends_on="2026-01-02",
    )
    entrant = old.get_model("raffle", "Entrant")
    for i, email in enumerate(["ada@x.test", "ada+1@x.test", "bo@bücher.test"]):
        entrant.objects.create(conference=conference, name=f"n{i}", email=email)
    assert set(entrant.objects.values_list("email_key", flat=True)) == {None}

    executor = MigrationExecutor(connection)
    executor.migrate(_AFTER)
    new = executor.loader.project_state(_AFTER).apps.get_model("raffle", "Entrant")
    assert sorted(new.objects.values_list("email_key", flat=True)) == [
        "ada@x.test",
        "ada@x.test",
        "bo@xn--bcher-kva.test",
    ]
    # Existing aliases stay as separate rows; nothing is merged or deleted.
    assert new.objects.count() == 3
