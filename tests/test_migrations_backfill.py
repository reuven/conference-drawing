"""The email_key backfill keys rows that existed before the field did."""

import importlib
from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from conference_drawing.raffle.forms import EntryForm
from conference_drawing.raffle.models import Conference, Entrant

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
    for i, email in enumerate(
        ["ada@example.com", "ada+1@example.com", "bo@bücher.example"]
    ):
        entrant.objects.create(conference=conference, name=f"n{i}", email=email)
    assert set(entrant.objects.values_list("email_key", flat=True)) == {None}

    executor = MigrationExecutor(connection)
    executor.migrate(_AFTER)
    new = executor.loader.project_state(_AFTER).apps.get_model("raffle", "Entrant")
    assert sorted(new.objects.values_list("email_key", flat=True)) == [
        "ada@example.com",
        "ada@example.com",
        "bo@xn--bcher-kva.example",
    ]
    # Existing aliases stay as separate rows; nothing is merged or deleted.
    assert new.objects.count() == 3


@pytest.mark.django_db(transaction=True)
def test_alias_of_a_backfilled_row_is_refused(executor: MigrationExecutor) -> None:
    """Review focus: rows keyed by the backfill count as already entered."""
    executor.migrate(_BEFORE)
    old = executor.loader.project_state(_BEFORE).apps
    conference = old.get_model("raffle", "Conference").objects.create(
        name="C",
        slug="c",
        time_zone="UTC",
        starts_on="2026-01-01",
        ends_on="2026-01-02",
    )
    old.get_model("raffle", "Entrant").objects.create(
        conference=conference, name="Old", email="ada@example.com"
    )
    executor = MigrationExecutor(connection)
    executor.migrate(_AFTER)

    live = Conference.objects.get(pk=conference.pk)
    form = EntryForm(
        live, {"name": "Ada", "email": "ada+raffle@example.com", "followup_ack": "on"}
    )
    assert form.is_valid(), form.errors
    assert form.save(timezone.now()) is None
    assert Entrant.objects.count() == 1
