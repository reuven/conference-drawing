"""Delete a conference's entrants after wrap-up; keep drawings, prizes and counts.

Once Drip has every entrant the owner removes all personal data for the
conference. Entrants (and, by cascade, their picks) go; coupon `pick` and audit
`pick` links become NULL; admin-history rows about those entrants are deleted;
the conference's failed outbox jobs are deleted (so they cannot be retried) and
every remaining job's payload is replaced with `{"redacted": true}`.
Audit details hold ids and counts only, so nothing else needs redacting.

A coupon code keeps `used=True` so it is never handed out twice.

Refuses with `DrawingError` (reused rather than adding a new class) unless the
conference is wrapped up, has not already been deleted, and has no pending
Drip jobs, because a pending job still needs its payload to be sent.

SQLite keeps deleted rows in free pages and the write-ahead log, so after the
commit we VACUUM and then checkpoint-truncate the WAL, best effort and outside the
transaction. The hourly snapshots (Task 16) still hold the data until they age
out, 48 hours later.

Logs carry ids and counts only.
"""

import datetime
import logging

from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, connection, transaction
from django.db.models import CharField
from django.db.models.functions import Cast

from .draw import DrawingError
from .drip.outbox import pending_count
from .models import Conference, Entrant, OutboxJob

logger = logging.getLogger(__name__)


def delete_entrants(conference: Conference, *, now: datetime.datetime) -> int:
    """Delete every entrant of `conference` and return how many were deleted.

    Use after wrap-up, once Drip has the entrants. `now` is accepted for parity
    with the other services; nothing time-dependent is stored.
    """
    with transaction.atomic():
        locked = Conference.objects.select_for_update().get(pk=conference.pk)
        if locked.wrapped_up_at is None:
            raise DrawingError(
                "The conference must be wrapped up before deleting entrants."
            )
        if locked.deleted_entrant_count is not None:
            raise DrawingError("Entrants were already deleted.")
        if pending_count(locked) > 0:
            raise DrawingError(
                "Drip jobs are still pending. Wait for them to send first."
            )
        entrants = Entrant.objects.filter(conference=locked)
        count = entrants.count()
        # Admin edits log each entrant's name (and sometimes email) in the
        # admin history; drop those rows with the people they describe.
        # A subquery, so a big conference cannot hit SQLite's variable limit.
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Entrant),
            object_id__in=entrants.annotate(oid=Cast("pk", CharField())).values("oid"),
        ).delete()
        # Picks go via the cascade; SET_NULL clears coupon and audit links.
        entrants.delete()
        # Failed jobs go entirely (a redacted one could be retried and fail as
        # poison); the rest (sent) keep a row but lose their payload.
        jobs = OutboxJob.objects.filter(conference=locked)
        jobs.filter(status=OutboxJob.Status.FAILED).delete()
        jobs.update(payload={"redacted": True})
        locked.deleted_entrant_count = count
        locked.save(update_fields=["deleted_entrant_count"])
    conference.deleted_entrant_count = count
    _scrub_sqlite(locked.pk)
    logger.info("deleted entrants conference=%s count=%s", locked.pk, count)
    return count


def _scrub_sqlite(conference_id: int) -> None:
    """VACUUM, then checkpoint-truncate the WAL, so deleted rows leave the files.

    The order matters: in WAL mode VACUUM writes the rebuilt pages into the WAL,
    so only a checkpoint after it copies them over the old pages in the main file.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("VACUUM")
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except OperationalError as exc:
        logger.warning(
            "deletion scrub failed conference=%s error=%s",
            conference_id,
            type(exc).__name__,
        )
