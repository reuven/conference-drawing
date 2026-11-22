"""Delete a conference's entrants after wrap-up; keep drawings, prizes and counts.

Once Drip has every entrant the owner removes all personal data for the
conference. Entrants (and, by cascade, their picks) go; coupon `pick` and audit
`pick` links become NULL; admin-history rows about those entrants are deleted;
the conference's failed outbox jobs are deleted (so they cannot be retried) and
every remaining job's payload is replaced with `{"redacted": true}`.
Audit details hold ids and counts only, so nothing else needs redacting.

A coupon code keeps `used=True` so it is never handed out twice.

Refuses with `DrawingError` (reused rather than adding a new class) unless the
conference is wrapped up, has no open drawing, has not already been deleted,
and has no pending Drip jobs, because a pending job still needs its payload to
be sent.

PostgreSQL's DELETE only marks tuples dead; their bytes stay in the table files
until something rewrites them. So once the deleting transaction commits we run
`VACUUM (FULL, ANALYZE)` on the tables that held personal data, best effort and
outside any transaction (VACUUM refuses to run inside one). VACUUM FULL writes
each table to a new file and drops the old one, but it cannot guarantee the
bytes are gone everywhere: WAL segments keep the deleted rows until they are
recycled, an old snapshot can keep them alive until it ends, and the hourly
pg_dump snapshots (Task 16) hold the data until they age out, 48 hours later.
VACUUM FULL takes an exclusive lock on each table while it rewrites it, so the
booth waits a moment; the tables are small. It also has to wait for every
reader of a table, such as a running pg_dump snapshot, so it gives up after
`LOCK_TIMEOUT` instead of stalling the request: that is logged as a scrub
failure (exception class only), the deletion itself stands, and the dead
tuples stay until autovacuum or the next VACUUM FULL.

Logs carry ids and counts only.
"""

import datetime
import functools
import logging

from django.contrib.admin.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, connection, transaction
from django.db.models import CharField
from django.db.models.functions import Cast

from .draw import DrawingError
from .drip.outbox import pending_count
from .models import Conference, CouponCode, Drawing, Entrant, OutboxJob, Pick

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
        if Drawing.objects.filter(
            conference=locked, status=Drawing.Status.OPEN
        ).exists():
            raise DrawingError("A drawing is still open. Close it before deleting.")
        if pending_count(locked) > 0:
            raise DrawingError(
                "Drip jobs are still pending. Wait for them to send first."
            )
        entrants = Entrant.objects.filter(conference=locked)
        count = entrants.count()
        # Admin edits log each entrant's name (and sometimes email) in the
        # admin history; drop those rows with the people they describe.
        # A subquery, so a big conference never builds a huge IN list.
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
        # After the outermost commit, outside any transaction, even when a
        # caller wraps this in its own atomic block.
        transaction.on_commit(functools.partial(_vacuum_full, locked.pk))
    conference.deleted_entrant_count = count
    logger.info("deleted entrants conference=%s count=%s", locked.pk, count)
    return count


# Every table that held an entrant's name, email or answers.
_SCRUBBED_MODELS = (Entrant, Pick, OutboxJob, CouponCode, LogEntry)
# How long VACUUM FULL may wait for a table lock (a PostgreSQL interval).
LOCK_TIMEOUT = "5s"


def _vacuum_full(conference_id: int) -> None:
    """Rewrite the tables that held personal data, so dead tuples leave the files.

    Runs from `on_commit`, in autocommit mode: VACUUM cannot run in a
    transaction block. The app role owns these tables (it ran migrate), which
    VACUUM FULL requires. Each table lock is waited for at most
    `LOCK_TIMEOUT`; the setting is reset afterwards because the connection is
    reused (CONN_MAX_AGE). A failure is logged by exception class only.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, false)", [LOCK_TIMEOUT]
            )
            try:
                for model in _SCRUBBED_MODELS:
                    table = connection.ops.quote_name(model._meta.db_table)
                    cursor.execute(f"VACUUM (FULL, ANALYZE) {table}")
            finally:
                cursor.execute("RESET lock_timeout")
    except DatabaseError as exc:
        logger.warning(
            "deletion scrub failed conference=%s error=%s",
            conference_id,
            type(exc).__name__,
        )
