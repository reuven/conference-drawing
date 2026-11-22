"""Deleting a conference's entrants: personal data goes, drawings and counts stay."""

import datetime
import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import Client
from django.urls import reverse

from conference_drawing.raffle import deletion
from conference_drawing.raffle.deletion import delete_entrants
from conference_drawing.raffle.draw import DrawingError, start_drawing
from conference_drawing.raffle.drip.outbox import requeue_failed
from conference_drawing.raffle.models import (
    AuditEvent,
    Conference,
    CouponCode,
    Drawing,
    Entrant,
    OutboxJob,
    Pick,
    PrizeLine,
)
from tests import factories

NOW = datetime.datetime(2027, 5, 12, 18, 0, tzinfo=datetime.UTC)
EARLY = NOW - datetime.timedelta(days=1)
SECRET_ANSWER = "zebra-quokka-answer"  # noqa: S105
SECRET_MAIL = "secret-mail.example"  # noqa: S105


@pytest.fixture
def conference(conference_factory: Callable[..., Conference]) -> Conference:
    return conference_factory(slug="pycon", name="PyCon US", wrapped_up_at=NOW)


@pytest.fixture
def populated(conference: Conference) -> dict[str, Any]:
    """A wrapped-up conference: 3 entrants, one digital pool pick holding a code."""
    people = [
        factories.make_entrant(
            conference,
            name=f"Zelda Quux{i}",
            email=f"zq{i}@{SECRET_MAIL}",
            answers={"want_to_learn": f"{SECRET_ANSWER}{i}"},
            created_at=EARLY,
        )
        for i in range(3)
    ]
    drawing = factories.make_drawing(
        conference, scheduled_at=NOW - datetime.timedelta(hours=2)
    )
    line = factories.make_prize_line(
        drawing,
        kind=PrizeLine.Kind.DIGITAL,
        url_template="https://example.com/redeem/{code}",
        code_mode=PrizeLine.CodeMode.POOL,
    )
    CouponCode.objects.create(prize_line=line, code="CODE-A")
    # The drawing ran before wrap-up; reproduce that order.
    Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=None)
    picks = start_drawing(
        drawing, now=NOW - datetime.timedelta(hours=1), rng=random.Random(1)
    )
    Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=NOW)
    OutboxJob.objects.create(
        conference=conference,
        kind=OutboxJob.Kind.EVENT,
        payload={"email": f"zq0@{SECRET_MAIL}", "name": "Zelda Quux0"},
        status=OutboxJob.Status.SENT,
    )
    return {"people": people, "drawing": drawing, "line": line, "picks": picks}


def _dump_database() -> str:
    """Every row of every table, Django's own included, as one string."""
    with connection.cursor() as cur:
        parts = []
        for table in connection.introspection.table_names(cur):
            cur.execute(f"SELECT * FROM {connection.ops.quote_name(table)}")  # noqa: S608
            parts.append(f"{table}: {cur.fetchall()!r}")
    return "\n".join(parts)


def test_refuses_with_pending_jobs(
    conference: Conference, populated: dict[str, Any]
) -> None:
    OutboxJob.objects.create(
        conference=conference, kind=OutboxJob.Kind.EVENT, payload={"a": 1}
    )
    with pytest.raises(DrawingError, match="pending"):
        delete_entrants(conference, now=NOW)
    assert Entrant.objects.filter(conference=conference).count() == 3
    conference.refresh_from_db()
    assert conference.deleted_entrant_count is None


def test_refuses_before_wrapup(conference_factory: Callable[..., Conference]) -> None:
    conf = conference_factory()
    factories.make_entrant(conf)
    with pytest.raises(DrawingError, match="wrapped up"):
        delete_entrants(conf, now=NOW)
    assert Entrant.objects.count() == 1


def test_deletes_entrants_and_picks_keeps_drawings_and_counts(
    conference: Conference, populated: dict[str, Any]
) -> None:
    lines_before = PrizeLine.objects.count()
    assert delete_entrants(conference, now=NOW) == 3
    assert Entrant.objects.count() == 0
    assert Pick.objects.count() == 0
    assert Drawing.objects.filter(conference=conference).count() == 1
    assert PrizeLine.objects.count() == lines_before
    assert AuditEvent.objects.filter(drawing=populated["drawing"]).exists()
    assert not AuditEvent.objects.filter(pick__isnull=False).exists()
    conference.refresh_from_db()
    assert conference.deleted_entrant_count == 3


def test_outbox_payloads_redacted(
    conference: Conference, populated: dict[str, Any]
) -> None:
    other = factories.make_conference()
    OutboxJob.objects.create(
        conference=other, kind=OutboxJob.Kind.EVENT, payload={"keep": 1}
    )
    delete_entrants(conference, now=NOW)
    mine = OutboxJob.objects.filter(conference=conference)
    assert mine.count() == 1
    assert all(j.payload == {"redacted": True} for j in mine)
    assert OutboxJob.objects.get(conference=other).payload == {"keep": 1}


def test_failed_jobs_are_removed_and_cannot_be_retried(
    conference: Conference, populated: dict[str, Any]
) -> None:
    OutboxJob.objects.create(
        conference=conference,
        kind=OutboxJob.Kind.SUBSCRIBERS,
        payload={"subscribers": [{"email": f"zq1@{SECRET_MAIL}"}]},
        status=OutboxJob.Status.FAILED,
        last_error_class="DripError",
    )
    other = factories.make_conference()
    OutboxJob.objects.create(
        conference=other, kind=OutboxJob.Kind.EVENT, status=OutboxJob.Status.FAILED
    )
    delete_entrants(conference, now=NOW)
    mine = OutboxJob.objects.filter(conference=conference)
    assert not mine.filter(status=OutboxJob.Status.FAILED).exists()
    assert requeue_failed(mine, now=NOW) == 0
    assert OutboxJob.objects.filter(conference=other).count() == 1


def test_assigned_code_must_be_marked_used(
    conference: Conference, populated: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        CouponCode.objects.update(used=False)


def test_no_email_left_anywhere_in_db(
    conference: Conference, populated: dict[str, Any]
) -> None:
    user = User.objects.create_user("owner", password="pw")  # noqa: S106
    LogEntry.objects.create(
        user=user,
        content_type=ContentType.objects.get_for_model(Entrant),
        object_id=str(populated["people"][0].pk),
        object_repr=f"Zelda Quux0 zq0@{SECRET_MAIL}",
        action_flag=ADDITION,
        change_message="Zelda Quux0",
    )
    keeper = factories.make_entrant(factories.make_conference(), name="Keeper")
    LogEntry.objects.create(
        user=user,
        content_type=ContentType.objects.get_for_model(Entrant),
        object_id=str(keeper.pk),
        object_repr="Keeper",
        action_flag=ADDITION,
    )
    assert SECRET_MAIL in _dump_database()
    delete_entrants(conference, now=NOW)
    dump = _dump_database()
    for person in populated["people"]:
        assert person.email not in dump
        assert person.name not in dump
    assert SECRET_MAIL not in dump
    assert SECRET_ANSWER not in dump
    assert "Zelda" not in dump
    # Another conference's admin history is left alone.
    assert LogEntry.objects.get().object_repr == "Keeper"


def test_other_conferences_entrants_untouched(
    conference: Conference, populated: dict[str, Any]
) -> None:
    other = factories.make_conference()
    keeper = factories.make_entrant(other, name="Keeper", answers={"a": "kept"})
    delete_entrants(conference, now=NOW)
    keeper.refresh_from_db()
    assert keeper.name == "Keeper"
    assert keeper.answers == {"a": "kept"}
    other.refresh_from_db()
    assert other.deleted_entrant_count is None


def test_codes_stay_used_after_deletion(
    conference: Conference, populated: dict[str, Any]
) -> None:
    code = CouponCode.objects.get()
    assert code.pick_id is not None
    delete_entrants(conference, now=NOW)
    code.refresh_from_db()
    assert code.pick_id is None
    assert code.used is True
    # Re-open the drawing as if new: the used code must not be handed out again.
    Conference.objects.filter(pk=conference.pk).update(wrapped_up_at=None)
    Drawing.objects.filter(pk=populated["drawing"].pk).update(
        status=Drawing.Status.SCHEDULED
    )
    factories.make_entrant(conference, created_at=EARLY)
    with pytest.raises(DrawingError, match="needs 1 codes, has 0"):
        start_drawing(
            Drawing.objects.get(pk=populated["drawing"].pk),
            now=NOW,
            rng=random.Random(2),
        )


def test_deleting_twice_is_refused(
    conference: Conference, populated: dict[str, Any]
) -> None:
    delete_entrants(conference, now=NOW)
    with pytest.raises(DrawingError, match="already deleted"):
        delete_entrants(conference, now=NOW)
    conference.refresh_from_db()
    assert conference.deleted_entrant_count == 3


@pytest.mark.django_db(transaction=True)
def test_scrub_failure_is_logged_by_class_only(
    conference: Conference,
    populated: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = MagicMock()
    fake.cursor.return_value.__enter__.return_value.execute.side_effect = (
        OperationalError(f"message-sentinel-3f9c {SECRET_MAIL}")
    )
    with (
        caplog.at_level(logging.INFO),
        patch("conference_drawing.raffle.deletion.connection", fake),
    ):
        delete_entrants(conference, now=NOW)
    assert Entrant.objects.count() == 0
    text = caplog.text
    assert "OperationalError" in text
    assert "message-sentinel-3f9c" not in text  # the class name only
    assert SECRET_MAIL not in text
    assert "Zelda" not in text
    assert f"conference={conference.pk}" in text


# The tables that held personal data, which VACUUM FULL must rewrite.
SCRUBBED = (Entrant, Pick, OutboxJob, CouponCode, LogEntry)


def _filenodes() -> dict[str, int]:
    with connection.cursor() as cur:
        nodes = {}
        for model in SCRUBBED:
            table = model._meta.db_table
            cur.execute("SELECT pg_relation_filenode(%s::regclass)", [table])
            nodes[table] = cur.fetchone()[0]
    return nodes


@pytest.mark.django_db(transaction=True)
def test_deletion_rewrites_the_tables_that_held_personal_data(
    conference: Conference, populated: dict[str, Any]
) -> None:
    """VACUUM FULL writes each table to a new file, leaving dead tuples behind.

    A plain DELETE only marks tuples dead; their bytes stay in the old file
    until it is rewritten. A new filenode proves the rewrite happened.
    """
    before = _filenodes()
    delete_entrants(conference, now=NOW)
    after = _filenodes()
    assert {t: before[t] != after[t] for t in before} == dict.fromkeys(before, True)


def test_scrub_waits_for_the_commit(
    conference: Conference, populated: dict[str, Any]
) -> None:
    """Inside a caller's transaction the VACUUM is deferred, never attempted.

    VACUUM cannot run in a transaction block; trying would abort the caller's
    transaction. The deletion itself still happens.
    """
    before = _filenodes()
    with transaction.atomic():
        delete_entrants(conference, now=NOW)
    assert Entrant.objects.count() == 0
    assert _filenodes() == before


@pytest.mark.django_db(transaction=True)
def test_scrub_runs_only_after_the_outer_transaction_commits(
    conference: Conference, populated: dict[str, Any]
) -> None:
    before = _filenodes()
    with transaction.atomic():
        delete_entrants(conference, now=NOW)
        assert _filenodes() == before  # still inside: nothing rewritten yet
    after = _filenodes()
    assert {t: before[t] != after[t] for t in before} == dict.fromkeys(before, True)


@pytest.mark.django_db(transaction=True)
def test_a_blocked_vacuum_gives_up_instead_of_stalling_the_request(
    conference: Conference,
    populated: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pg_dump (or any reader) holding ACCESS SHARE on a table makes VACUUM
    FULL wait; lock_timeout turns that wait into the class-name warning."""
    monkeypatch.setattr(deletion, "LOCK_TIMEOUT", "200ms")
    locked, release = threading.Event(), threading.Event()

    def hold_access_share() -> None:
        try:
            with transaction.atomic():
                list(Entrant.objects.values_list("pk", flat=True)[:1])
                locked.set()
                release.wait(timeout=10)
        finally:
            connection.close()

    holder = threading.Thread(target=hold_access_share)
    holder.start()
    try:
        assert locked.wait(timeout=5)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            assert delete_entrants(conference, now=NOW) == 3
        waited = time.monotonic() - started
    finally:
        release.set()
        holder.join()
    assert waited < 5
    assert "deletion scrub failed" in caplog.text
    assert "error=OperationalError" in caplog.text
    assert Entrant.objects.count() == 0
    # The connection is usable afterwards, with the timeout reset.
    with connection.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone() == ("0",)


# --- view --------------------------------------------------------------------


@pytest.fixture
def owner(client: Client, db: None) -> Client:
    user = User.objects.create_user("owner", password="pw", is_staff=True)  # noqa: S106
    client.force_login(user)
    return client


def test_view_requires_login(client: Client, conference: Conference) -> None:
    url = reverse("booth-delete-entrants", args=["pycon"])
    assert client.get(url).status_code == 302
    assert client.post(url, {"slug": "pycon"}).status_code == 302
    conference.refresh_from_db()
    assert conference.deleted_entrant_count is None


def test_confirmation_page_shows_counts_and_failed_jobs(
    owner: Client, conference: Conference, populated: dict[str, Any]
) -> None:
    OutboxJob.objects.create(
        conference=conference,
        kind=OutboxJob.Kind.EVENT,
        payload={"x": 1},
        status=OutboxJob.Status.FAILED,
    )
    body = owner.get(reverse("booth-delete-entrants", args=["pycon"])).content.decode()
    assert "3 entrants" in body
    assert "1 failed Drip job will be discarded" in body
    assert Entrant.objects.count() == 3


def test_confirmation_page_after_deletion_and_before_wrapup(
    owner: Client, conference: Conference, populated: dict[str, Any]
) -> None:
    delete_entrants(conference, now=NOW)
    body = owner.get(reverse("booth-delete-entrants", args=["pycon"])).content.decode()
    assert "already deleted" in body
    fresh = factories.make_conference(slug="fresh")
    body = owner.get(reverse("booth-delete-entrants", args=["fresh"])).content.decode()
    assert "wrapped up" in body
    assert fresh.wrapped_up_at is None


@pytest.mark.parametrize("typed", ["", "wrong", "PYCON", "pycon-x"])
def test_confirmation_requires_typed_slug(
    owner: Client, conference: Conference, populated: dict[str, Any], typed: str
) -> None:
    response = owner.post(
        reverse("booth-delete-entrants", args=["pycon"]), {"slug": typed}, follow=True
    )
    assert "Type the conference&#x27;s slug to confirm." in response.content.decode()
    assert Entrant.objects.count() == 3
    conference.refresh_from_db()
    assert conference.deleted_entrant_count is None


def test_typed_slug_is_stripped_and_deletes(
    owner: Client, conference: Conference, populated: dict[str, Any]
) -> None:
    response = owner.post(
        reverse("booth-delete-entrants", args=["pycon"]),
        {"slug": "  pycon \n"},
        follow=True,
    )
    assert Entrant.objects.count() == 0
    body = response.content.decode()
    assert "Entrants deleted (3)" in body
    assert reverse("booth-delete-entrants", args=["pycon"]) not in body


def test_refusal_is_a_message_not_a_500(
    owner: Client, conference: Conference, populated: dict[str, Any]
) -> None:
    OutboxJob.objects.create(
        conference=conference, kind=OutboxJob.Kind.EVENT, payload={"a": 1}
    )
    response = owner.post(
        reverse("booth-delete-entrants", args=["pycon"]), {"slug": "pycon"}, follow=True
    )
    assert response.status_code == 200
    assert "pending" in response.content.decode()
    assert Entrant.objects.count() == 3


def test_second_post_is_refused_cleanly(
    owner: Client, conference: Conference, populated: dict[str, Any]
) -> None:
    url = reverse("booth-delete-entrants", args=["pycon"])
    owner.post(url, {"slug": "pycon"})
    response = owner.post(url, {"slug": "pycon"}, follow=True)
    assert response.status_code == 200
    assert "already deleted" in response.content.decode()
    conference.refresh_from_db()
    assert conference.deleted_entrant_count == 3


def test_conference_page_links_delete_only_when_wrapped_up(
    owner: Client,
    conference: Conference,
    conference_factory: Callable[..., Conference],
) -> None:
    link = reverse("booth-delete-entrants", args=["pycon"])
    page = owner.get(reverse("booth-conference", args=["pycon"])).content.decode()
    assert link in page
    open_conf = conference_factory(slug="open")
    page = owner.get(reverse("booth-conference", args=["open"])).content.decode()
    assert reverse("booth-delete-entrants", args=[open_conf.slug]) not in page
