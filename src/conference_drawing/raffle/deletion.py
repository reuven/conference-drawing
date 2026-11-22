"""Delete a conference's entrants after wrap-up; keep drawings, prizes and counts.

Once Drip has every entrant the owner removes all personal data for the
conference. Entrants (and, by cascade, their picks) go; coupon `pick` and audit
`pick` links become NULL; admin-history rows about those entrants are deleted;
every outbox payload for the conference is replaced
with `{"redacted": true}` (failed jobs are discarded this way, not retried).
Audit details hold ids and counts only, so nothing else needs redacting.

A coupon code keeps `used=True` so it is never handed out twice.

Refuses with `DrawingError` (reused rather than adding a new class) unless the
conference is wrapped up, has not already been deleted, and has no pending
Drip jobs, because a pending job still needs its payload to be sent.

SQLite keeps deleted rows in free pages and the write-ahead log, so after the
commit we checkpoint-truncate the WAL and VACUUM, best effort and outside the
transaction. The hourly snapshots (Task 16) still hold the data until they age
out, 48 hours later.

Logs carry ids and counts only.
"""

import datetime
import logging

from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import OperationalError, connection, transaction

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
        LogEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(Entrant),
            object_id__in=[str(pk) for pk in entrants.values_list("pk", flat=True)],
        ).delete()
        # Picks go via the cascade; SET_NULL clears coupon and audit links.
        entrants.delete()
        OutboxJob.objects.filter(conference=locked).update(payload={"redacted": True})
        locked.deleted_entrant_count = count
        locked.save(update_fields=["deleted_entrant_count"])
    conference.deleted_entrant_count = count
    _scrub_sqlite(locked.pk)
    logger.info("deleted entrants conference=%s count=%s", locked.pk, count)
    return count


def _scrub_sqlite(conference_id: int) -> None:
    """Checkpoint-truncate the WAL and VACUUM so deleted rows leave the files."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            cursor.execute("VACUUM")
    except OperationalError as exc:
        logger.warning(
            "deletion scrub failed conference=%s error=%s",
            conference_id,
            type(exc).__name__,
        )
